"""1688 货源链接：抽取、归一、覆盖公共箱、有效性检查。

负责人 2026-11-20 两个关键约束决定了实现方向：

1. **载体是「生成一个然后一直复制」**，不是每次重新采集；
2. **一个妙手账号每天只能采集 10 个产品** —— 靠重新采集换链接根本不够用。

所以必须「**覆盖链接**」而不是「重新采集」。实测三条路径的结论：

    公共采集箱 edit_common_collect_box_detail  → ✅ 能改 sourceList
         （写入 826406158136 后回读一致，且不消耗采集配额）
    店铺采集箱 save_shop_collect_item_info     → ❌ 传了返回 success，
         但回读是 null，上游静默忽略
    get_shop_collect_item_info                → 根本没有 sourceList 字段

以及一个必须记住的坑：**妙手自己说「不存在的链接也会 success 并采到无关的
真实商品」**，且从服务器侧 HTTP 请求 1688 拿到的只是反爬壳页（存在与不存在
返回完全相同的字节），所以 HTTP 不能用来判死活。
"""
from __future__ import annotations

import pytest

VALID = "https://detail.1688.com/offer/717267894593.html"
OFFER = "717267894593"


# --- 抽取与归一 -------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (OFFER, OFFER),
    (VALID, OFFER),
    ("https://detail.1688.com/offer/717267894593.html", OFFER),
    ("https://m.1688.com/offer/717267894593.html?spm=abc", OFFER),
    ("https://detail.1688.com/offer/717267894593.html?offerId=717267894593", OFFER),
    ("717267894593.html", OFFER),
    ("http://detail.1688.com/offer/717267894593.html", OFFER),
    ("", ""),
    ("abc", ""),
    ("https://item.taobao.com/x", ""),
    ("123", ""),           # 太短，不是 offer id
])
def test_extract_offer_id(raw, expected):
    from services.source_link import extract_offer_id

    assert extract_offer_id(raw) == expected


@pytest.mark.parametrize("raw", [
    OFFER, VALID, "https://m.1688.com/offer/%s.html?spm=x" % OFFER,
    "%s.html" % OFFER,
])
def test_normalize_always_yields_standard_url(raw):
    """各种写法都归一成同一个标准商品页 —— 载体重建只认这一种。"""
    from services.source_link import normalize_source_url

    assert normalize_source_url(raw) == VALID


def test_normalize_keeps_non_1688_link():
    """负责人可能贴别的采购渠道，不是 1688 也照样保存，只是不做 offer 校验。"""
    from services.source_link import normalize_source_url

    other = "https://example.com/product/abc"
    assert normalize_source_url(other) == other
    assert normalize_source_url("not-a-url") == ""


# --- 从采集箱行抓取 ---------------------------------------------------------

def test_capture_from_box_row():
    from services.source_link import capture_from_box_row

    row = {
        "title": "冬季加绒保暖鞋",
        "sourceList": [{
            "source": "1688",
            "sourceItemId": OFFER,
            "sourceItemUrl": VALID,
        }],
    }
    captured = capture_from_box_row(row)
    assert captured["sourceUrl"] == VALID
    assert captured["sourceItemId"] == OFFER
    assert captured["sourceTitle"] == "冬季加绒保暖鞋"
    assert captured["sourceCapturedAt"] > 0


def test_capture_ignores_non_1688_source():
    from services.source_link import capture_from_box_row

    row = {"title": "x", "sourceList": [{"source": "taobao", "sourceItemId": OFFER}]}
    assert capture_from_box_row(row) == {}


def test_capture_returns_empty_for_missing():
    """抓不到就返回 {}，不能写空字段进 payload —— 否则分不清「没抓到」和「抓到是空」。"""
    from services.source_link import capture_from_box_row

    assert capture_from_box_row(None) == {}
    assert capture_from_box_row({}) == {}
    assert capture_from_box_row({"title": "x"}) == {}


def test_capture_handles_source_case_variants():
    """上游 `source` 大小写不统一（1688 / 1688.com / 1688CN 混着来）。"""
    from services.source_link import capture_from_box_row

    for variant in ("1688", "1688.com", "1688CN", "ALIBABA"):
        row = {"title": "x", "sourceList": [
            {"source": variant, "sourceItemId": OFFER}]}
        assert capture_from_box_row(row).get("sourceItemId") == OFFER, variant


# --- attach_source 只补缺 ----------------------------------------------------

def test_attach_source_does_not_overwrite_manual_value():
    """用户手改的链接比自动抓的可信 —— 不能覆盖。"""
    from services.source_link import attach_source

    payload = {"sourceUrl": "https://detail.1688.com/offer/111111111111.html"}
    changed = attach_source(payload, {"sourceUrl": VALID, "sourceItemId": OFFER})
    assert payload["sourceUrl"] == "https://detail.1688.com/offer/111111111111.html"
    assert payload["sourceItemId"] == OFFER   # 缺的字段仍然补上
    assert changed is True


def test_attach_source_noop_when_complete():
    from services.source_link import attach_source

    payload = {"sourceUrl": VALID, "sourceItemId": OFFER}
    assert attach_source(payload, {"sourceUrl": VALID, "sourceItemId": OFFER}) is False


# --- 公共箱 sourceList 构造 -------------------------------------------------

def test_build_source_list_shape():
    """实测上游接受的就是这五个字段（多一个少一个都可能被拒）。"""
    from services.source_link import build_source_list

    built = build_source_list(OFFER)
    assert len(built) == 1
    entry = built[0]
    assert entry["source"] == "1688"
    assert entry["sourceItemId"] == OFFER
    assert entry["sourceItemUrl"] == VALID
    assert entry["sourceSite"] == ""
    assert entry["remark"] is None


def test_build_source_list_rejects_garbage():
    from services.source_link import build_source_list

    assert build_source_list("") == []
    assert build_source_list("not a url") == []


# --- 覆盖公共箱（用假 client）------------------------------------------------

class FakeBoxClient:
    """模拟公共箱：记录 edit 调用，回读返回写进去的 sourceList。"""

    def __init__(self, *, current_offer: str = "999999999999",
                 stock: int = 623927, ignore_edit: bool = False):
        self.current_offer = current_offer
        self.stock = stock
        self.ignore_edit = ignore_edit
        self.edit_calls: list[dict] = []
        self.detail_calls = 0

    def common_box_detail(self, common_id):
        self.detail_calls += 1
        return {"data": {
            "ossMd5": "sig==",
            "editCommonCollectBoxDetail": {
                "commonCollectBoxDetailId": common_id,
                "title": "载体商品",
                "stock": self.stock,
                "sourceList": [] if self.ignore_edit and self.edit_calls else [{
                    "remark": None,
                    "source": "1688",
                    "sourceSite": "",
                    "sourceItemId": self.current_offer,
                    "sourceItemUrl": "https://detail.1688.com/offer/%s.html"
                                     % self.current_offer,
                }],
                "skuMap": {";红;39;": {"price": 22, "stock": self.stock}},
            },
        }}

    def edit_common_box_detail(self, payload):
        self.edit_calls.append(payload)
        if not self.ignore_edit:
            edit = payload.get("editCommonCollectBoxDetail") or {}
            written = edit.get("sourceList") or []
            if written:
                self.current_offer = str(written[0].get("sourceItemId") or "")
        return {"result": "success", "data": {"ossMd5": "newsig=="}}


def test_override_changes_source():
    from services.source_link import override_common_box_source

    client = FakeBoxClient(current_offer="999999999999")
    result = override_common_box_source(client, 4148378072, VALID)

    assert result["ok"] is True
    assert result["changed"] is True
    assert client.current_offer == OFFER
    # 写进去的 body 形状：cid + ossMd5 + editCommonCollectBoxDetail
    body = client.edit_calls[0]
    assert body["commonCollectBoxDetailId"] == 4148378072
    assert body["ossMd5"] == "sig=="
    assert body["editCommonCollectBoxDetail"]["sourceList"][0]["sourceItemId"] == OFFER


def test_override_caps_stock():
    """stock 超过 99999 会被上游整单拒绝 —— 必须夹住。

    实测报错：「editCommonCollectBoxDetail.stock 不能大于 99999」，
    而株洲鞋类的原始库存动辄 60 万。
    """
    from services.source_link import COMMON_BOX_STOCK_CAP, override_common_box_source

    client = FakeBoxClient(current_offer="999999999999", stock=623927)
    override_common_box_source(client, 4148378072, VALID)

    edit = client.edit_calls[0]["editCommonCollectBoxDetail"]
    assert edit["stock"] == COMMON_BOX_STOCK_CAP
    assert COMMON_BOX_STOCK_CAP == 99999
    for sku in edit["skuMap"].values():
        assert sku["stock"] <= COMMON_BOX_STOCK_CAP


def test_override_skips_when_already_correct():
    """已经是目标链接时不该白打一次 edit（每打一次都占上游配额）。"""
    from services.source_link import override_common_box_source

    client = FakeBoxClient(current_offer=OFFER)
    result = override_common_box_source(client, 4148378072, VALID)

    assert result["ok"] is True
    assert result["changed"] is False
    assert client.edit_calls == []


def test_override_detects_silently_ignored_write():
    """店铺接口会静默忽略 sourceList —— 所以必须回读校验。

    这个测试钉住「不能只看接口返回 success 就以为改上了」。
    """
    from services.source_link import override_common_box_source

    client = FakeBoxClient(current_offer="999999999999", ignore_edit=True)
    result = override_common_box_source(client, 4148378072, VALID)

    assert result["ok"] is False
    assert result["changed"] is False
    assert "回读" in result["error"], result["error"]


def test_override_rejects_bad_id_and_url():
    from services.source_link import override_common_box_source

    client = FakeBoxClient()
    assert override_common_box_source(client, "abc", VALID)["ok"] is False
    assert override_common_box_source(client, 4148378072, "garbage")["ok"] is False
    assert client.edit_calls == []


# --- 有效性检查 -------------------------------------------------------------

def test_check_missing_and_malformed():
    from services.source_link import (
        STATUS_MALFORMED,
        STATUS_MISSING,
        check_payload_source,
    )

    assert check_payload_source({})["status"] == STATUS_MISSING
    assert check_payload_source({"sourceUrl": "abc"})["status"] == STATUS_MALFORMED


def test_check_local_only_marks_ok():
    """不传 client 时只做本地格式检查，零请求。"""
    from services.source_link import STATUS_OK, check_payload_source

    result = check_payload_source({"sourceUrl": VALID})
    assert result["status"] == STATUS_OK
    assert result["offerId"] == OFFER


def test_verify_found_in_common_box_is_verified():
    from services.source_link import STATUS_VERIFIED, verify_offer_live

    class BoxClient:
        def common_box_list(self, **_kwargs):
            return {"data": {"detailList": [
                {"title": "冬季加绒保暖鞋",
                 "sourceList": [{"source": "1688", "sourceItemId": OFFER}]}]}}

    result = verify_offer_live(VALID, client=BoxClient())
    assert result["status"] == STATUS_VERIFIED
    assert result["title"] == "冬季加绒保暖鞋"


def test_verify_not_found_is_unknown_not_dead():
    """搜不到**不等于**失效 —— 可能只是这个账号没采过它。

    判成 dead 会让老师误删好链接，所以必须是 unknown。
    """
    from services.source_link import STATUS_UNKNOWN, verify_offer_live

    class EmptyClient:
        def common_box_list(self, **_kwargs):
            return {"data": {"detailList": []}}

    result = verify_offer_live(VALID, client=EmptyClient())
    assert result["status"] == STATUS_UNKNOWN
    assert "不代表失效" in result["error"]


def test_verify_without_client_is_unknown():
    from services.source_link import STATUS_UNKNOWN, verify_offer_live

    assert verify_offer_live(VALID, client=None)["status"] == STATUS_UNKNOWN


def test_verify_handles_upstream_error():
    """上游报错也不能判死。"""
    from services.source_link import STATUS_UNKNOWN, verify_offer_live

    class BoomClient:
        def common_box_list(self, **_kwargs):
            raise RuntimeError("gateway timeout")

    assert verify_offer_live(VALID, client=BoomClient())["status"] == STATUS_UNKNOWN


def test_check_payload_source_with_client_uses_live():
    from services.source_link import STATUS_VERIFIED, check_payload_source

    class BoxClient:
        def common_box_list(self, **_kwargs):
            return {"data": {"detailList": [
                {"title": "t", "sourceList": [
                    {"source": "1688", "sourceItemId": OFFER}]}]}}

    result = check_payload_source({"sourceUrl": VALID}, client=BoxClient())
    assert result["status"] == STATUS_VERIFIED
    assert result["url"] == VALID