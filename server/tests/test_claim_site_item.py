"""site 模式商品自动认领的测试。

## 为什么需要这个

负责人要求：「自动化处理不能默认给他选择一个店铺吗，加到文字处理里面」

妙手采集箱有两类商品：
  · shop —— 已分配到店铺，有 shopId，能直接处理
  · site —— 只采集到采集箱、还没有店铺，shopId 为空，原来**无法处理**

实测确认 `claim_to_shop([detailId], [shopId])` 对 site 商品有效：

    认领前: editModel=site, shopId=''       key=3549599651:site:TIKTOK
    claim_to_shop([3549599651], [19974446]) → {'result': 'success'}
    认领后: editModel=shop, shopId=19974446  key=3549599651:19974446

所以 process 路由在 shopId 为空时会先认领一步。

运行：
    cd miaoshou/server && python -m pytest tests/test_claim_site_item.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from core import errors                       # noqa: E402
from services import collect_box_service       # noqa: E402


class FakeClient:
    """记录 claim_to_shop 调用，可配置抛错。"""

    def __init__(self, *, fail_with: str | None = None) -> None:
        self.calls: list[tuple[list[int], list[int]]] = []
        self.fail_with = fail_with
        self.closed = False

    def claim_to_shop(self, detail_ids: list[int], shop_ids: list[int]) -> dict:
        self.calls.append((list(detail_ids), list(shop_ids)))
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with)
        return {"result": "success", "code": "success", "data": None}

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def patched(monkeypatch):
    """替换 account_client 和 require_shop，避免真打上游 / 查库。"""
    state: dict = {"client": FakeClient(), "required": []}

    def fake_account_client(account):
        return state["client"]

    def fake_require_shop(db, account, shop_id):
        state["required"].append(str(shop_id))
        return {"shopId": str(shop_id)}

    monkeypatch.setattr(collect_box_service, "account_client", fake_account_client)
    monkeypatch.setattr(collect_box_service, "require_shop", fake_require_shop)
    return state


def call(detail_id="3549599651", target="19974446"):
    """调用 claim_site_item，db / account 用占位符（被 mock 掉了）。"""
    return collect_box_service.claim_site_item(
        None, object(), detail_id=str(detail_id), target_shop_id=str(target))


# --- 正常路径 -------------------------------------------------------------

def test_claims_and_returns_shop_id(patched) -> None:
    result = call()
    assert result == "19974446"


def test_calls_claim_to_shop_with_single_detail_and_shop(patched) -> None:
    call()
    assert patched["client"].calls == [([3549599651], [19974446])]


def test_validates_target_shop_belongs_to_account(patched) -> None:
    """目标店铺必须属于该账号，否则上游会失败且错误难懂。"""
    call(target="19974446")
    assert patched["required"] == ["19974446"]


def test_closes_client(patched) -> None:
    call()
    assert patched["client"].closed is True


# --- 参数校验 -------------------------------------------------------------

def test_rejects_empty_target_shop(patched) -> None:
    with pytest.raises(errors.AppError):
        call(target="")


def test_rejects_blank_target_shop(patched) -> None:
    with pytest.raises(errors.AppError):
        call(target="   ")


def test_rejects_non_numeric_detail_id(patched) -> None:
    with pytest.raises(errors.AppError):
        call(detail_id="abc")


def test_rejection_happens_before_any_upstream_call(patched) -> None:
    """参数不合法时不能先打上游 —— 认领是不可撤销动作。"""
    with pytest.raises(errors.AppError):
        call(target="")
    assert patched["client"].calls == []


# --- 幂等 ---------------------------------------------------------------

def test_already_claimed_is_not_an_error(monkeypatch, patched) -> None:
    """已经在目标店铺里时上游会报错，但那是幂等成功，不该冒泡成失败。"""
    patched["client"].fail_with = "该商品已在采集箱中"

    # 让服务层把它包成 MiaoshouError 的样子
    from services.miaoshou import MiaoshouError

    def raise_claimed(*_args, **_kwargs):
        raise MiaoshouError("该商品已在采集箱中")

    patched["client"].claim_to_shop = raise_claimed

    assert call() == "19974446"


def test_other_upstream_error_is_wrapped(monkeypatch, patched) -> None:
    """真正的失败要变成可读的上游错误，不能原样抛 RuntimeError。"""
    from services.miaoshou import MiaoshouError

    def raise_other(*_args, **_kwargs):
        raise MiaoshouError("商品不存在")

    patched["client"].claim_to_shop = raise_other

    with pytest.raises(errors.AppError) as excinfo:
        call()
    assert "认领到店铺失败" in str(excinfo.value)