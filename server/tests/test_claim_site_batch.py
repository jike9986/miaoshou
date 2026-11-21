"""批量认领 site 商品到目标店铺的测试。

## 功能背景

负责人要求：「打开这个软件如果有未分配的自动分配，在还没有开始自动化处理
就把这些产品全部自动弄好」。

也就是启动时 `claim_site_batch` 一次把采集箱里所有 site 商品分好店。

## 这里钉住的行为

1. 分批调用（每批 SITE_CLAIM_BATCH 件），不能一次塞 200 个给上游
2. 已分好店的商品不出现在 site 列表里，天然跳过
3. 单批失败不整体中断 —— 启动阶段不能因为一条脏数据放弃所有商品
4. 上游说「已在目标店铺」当幂等成功
5. `max_items` 上限生效
6. `target_shop_id` 空 → 明确校验错误，不打上游

运行：
    cd miaoshou/server && python -m pytest tests/test_claim_site_batch.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from core import errors                                 # noqa: E402
from services import collect_box_service                # noqa: E402
from services.collect_box_service import SITE_CLAIM_BATCH  # noqa: E402


class FakeClient:
    """记录 claim_to_shop 调用；可让指定批次抛错。"""

    def __init__(self, rows: list[dict], *,
                 fail_batches: set[int] | None = None,
                 already_claimed: bool = False) -> None:
        self.rows = rows
        self.fail_batches = fail_batches or set()
        self.already_claimed = already_claimed
        self.calls: list[tuple[list[int], list[int]]] = []
        self.closed = False
        self._batch_index = 0

    def collect_box_list(self, **_kwargs) -> dict:
        return {"data": {"detailList": self.rows}}

    def claim_to_shop(self, detail_ids: list[int], shop_ids: list[int]) -> dict:
        self.calls.append((list(detail_ids), list(shop_ids)))
        index = self._batch_index
        self._batch_index += 1
        if index in self.fail_batches:
            from services.miaoshou import MiaoshouError
            raise MiaoshouError("商品不存在")
        if self.already_claimed:
            from services.miaoshou import MiaoshouError
            raise MiaoshouError("该商品已在该店铺的采集箱中")
        return {"result": "success"}

    def close(self) -> None:
        self.closed = True


def site_row(detail_id: int) -> dict:
    return {
        "editModel": "site",
        "collectBoxDetailId": detail_id,
        "title": f"商品 {detail_id}",
        "thumbnail": "https://example.invalid/x.jpg",
    }


def shop_row(detail_id: int, shop_id: int = 111) -> dict:
    return {
        "editModel": "shop",
        "collectBoxDetailId": detail_id,
        "collectBoxDetailShopList": [{"shopId": shop_id, "shopName": "已有店"}],
        "title": f"已分配 {detail_id}",
    }


@pytest.fixture()
def patched(monkeypatch):
    state: dict = {"client": None}

    def fake_account_client(_account):
        return state["client"]

    def fake_require_shop(_db, _account, shop_id):
        return {"shopId": str(shop_id)}

    def fake_invalidate(_account):
        return 1

    monkeypatch.setattr(collect_box_service, "account_client", fake_account_client)
    monkeypatch.setattr(collect_box_service, "require_shop", fake_require_shop)
    monkeypatch.setattr(collect_box_service, "invalidate_account", fake_invalidate)
    return state


def run(patched, rows, *, target="19974446", max_items=200):
    patched["client"] = FakeClient(rows)
    return collect_box_service.claim_site_batch(
        None, object(), target_shop_id=target, max_items=max_items)


# --- 基本行为 -------------------------------------------------------------

def test_claims_all_site_items(patched) -> None:
    result = run(patched, [site_row(1), site_row(2), site_row(3)])
    assert result["claimed"] == 3
    assert result["failed"] == 0
    assert result["total"] == 3


def test_ignores_already_assigned_items(patched) -> None:
    """已分好店的不在 site 列表里，不该被再认领一次。"""
    result = run(patched, [site_row(1), shop_row(2)])
    assert result["claimed"] == 1
    assert result["total"] == 1
    assert patched["client"].calls == [([1], [19974446])]


def test_no_site_items_makes_no_upstream_call(patched) -> None:
    result = run(patched, [shop_row(1)])
    assert result["claimed"] == 0
    assert result["total"] == 0
    assert patched["client"].calls == []


def test_uses_target_shop(patched) -> None:
    run(patched, [site_row(1)], target="20075695")
    assert patched["client"].calls == [([1], [20075695])]


def test_invalidates_cache_after_claim(patched, monkeypatch) -> None:
    called = {"count": 0}

    def fake_invalidate(_account):
        called["count"] += 1
        return 1

    monkeypatch.setattr(collect_box_service, "invalidate_account", fake_invalidate)
    run(patched, [site_row(1)])
    assert called["count"] == 1


def test_closes_client(patched) -> None:
    run(patched, [site_row(1)])
    assert patched["client"].closed is True


# --- 分批 ---------------------------------------------------------------

def test_batches_respect_batch_size(patched) -> None:
    """44 件要分 3 批（20+20+4），不能一次塞给上游。"""
    rows = [site_row(index) for index in range(44)]
    result = run(patched, rows)

    batches = [len(call[0]) for call in patched["client"].calls]
    assert batches == [20, 20, 4]
    assert result["claimed"] == 44


def test_batch_size_constant_is_sane(patched) -> None:
    assert 1 < SITE_CLAIM_BATCH <= 50


def test_max_items_caps_work(patched) -> None:
    rows = [site_row(index) for index in range(100)]
    result = run(patched, rows, max_items=10)
    assert result["claimed"] == 10
    assert result["total"] == 100


def test_detail_ids_filter(patched, monkeypatch) -> None:
    patched["client"] = FakeClient([site_row(1), site_row(2), site_row(3)])
    result = collect_box_service.claim_site_batch(
        None, object(), target_shop_id="19974446",
        detail_ids=["1", "3"])
    assert result["claimed"] == 2
    assert patched["client"].calls == [([1, 3], [19974446])]


# --- 容错 ---------------------------------------------------------------

def test_failed_batch_does_not_abort_others(patched) -> None:
    """启动阶段不能因为一条脏数据放弃所有商品。"""
    rows = [site_row(index) for index in range(40)]
    patched["client"] = FakeClient(rows, fail_batches={0})
    result = collect_box_service.claim_site_batch(
        None, object(), target_shop_id="19974446")

    # 第一批 20 件失败，后面两批照常
    assert result["failed"] == 20
    assert result["claimed"] == 20
    assert result["errors"]


def test_already_claimed_counts_as_success(patched) -> None:
    """上游说「已在店铺采集箱里」是幂等成功，不该计为失败。"""
    patched["client"] = FakeClient([site_row(1)], already_claimed=True)
    result = collect_box_service.claim_site_batch(
        None, object(), target_shop_id="19974446")
    assert result["claimed"] == 1
    assert result["failed"] == 0


# --- 参数校验 -----------------------------------------------------------

def test_rejects_empty_target_shop(patched) -> None:
    patched["client"] = FakeClient([])
    with pytest.raises(errors.AppError):
        collect_box_service.claim_site_batch(None, object(), target_shop_id="")


def test_blank_target_does_not_touch_upstream(patched) -> None:
    """参数不合法时不能打上游 —— 认领是不可撤销动作。"""
    patched["client"] = FakeClient([site_row(1)])
    with pytest.raises(errors.AppError):
        collect_box_service.claim_site_batch(None, object(), target_shop_id="  ")
    assert patched["client"].calls == []


# --- _is_already_claimed -------------------------------------------------

@pytest.mark.parametrize("message", [
    "该商品已在采集箱中",
    "已经在该店铺的采集箱里",
    "商品已存在",
    "item already exists in shop",
])
def test_is_already_claimed_true(message: str) -> None:
    assert collect_box_service._is_already_claimed(message) is True


@pytest.mark.parametrize("message", [
    "商品不存在",
    "网络错误",
    "",
])
def test_is_already_claimed_false(message: str) -> None:
    assert collect_box_service._is_already_claimed(message) is False