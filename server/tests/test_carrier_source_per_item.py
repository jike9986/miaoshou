"""每件商品要有自己的货源链接（负责人 2026-11-20 明确要求）。

## 为什么这个测试必须存在

派发时一批 50 件是 **50 个不同商品**，各自对应不同的 1688 货源。
但 `collect_carriers` 整轮复制都用**同一个 `common_id`**：

    while pending_copies > 0:
        for _ in range(pending_copies):
            client.claim_common_to_platform([(common_id, "tiktok", 1)])
                                                     ^^^^^^^^^^ 一直是同一个

而 `claim_common_to_platform` 复制的是**当时的快照** —— 复制完再改公共箱，
已经复制出去的副本不会跟着变。

所以如果在循环外只改一次链接，这一批 50 件会**共用同一条货源链接**，
采购员出单后还是分不清哪件对哪条。必须每件复制前改一次。
"""
from __future__ import annotations

import pytest


class LinkAwareClaimClient:
    """模拟「复制是快照」的上游：每次复制记下当时的公共箱链接。"""

    def __init__(self, *, start_offer: str = "111111111111"):
        self.current: list[dict] = [{
            "remark": None, "source": "1688", "sourceSite": "",
            "sourceItemId": start_offer,
            "sourceItemUrl": "https://detail.1688.com/offer/%s.html" % start_offer,
        }]
        self.edit_calls: list[str] = []
        self.detail_calls = 0
        self._oss = "oss-0"
        self._seq = 0
        # 每次复制产出的 detailId → 复制当时的链接快照
        self.snapshots: dict[int, str] = {}

    # --- 上游接口 ---
    def common_box_list(self, *, page_no=1, page_size=20, tab="",
                        source_item_keyword=""):
        # 公共箱里有一件可用载体（status=success 才会被选）
        return {"data": {"detailList": [{
            "commonCollectBoxDetailId": 4148378072,
            "status": "success",
            "title": "载体",
            "sourceList": list(self.current),
        }]}}

    def common_box_detail(self, common_id):
        self.detail_calls += 1
        return {"data": {
            "ossMd5": self._oss,
            "editCommonCollectBoxDetail": {
                "commonCollectBoxDetailId": common_id,
                "title": "载体",
                "stock": 623927,          # 故意用会被拒的大库存
                "sourceList": list(self.current),
                "sourceMataInfo": {"source": "1688",
                                   "oriItemNum": self.current[0]["sourceItemId"]},
                "skuMap": {";a;b;": {"price": 20, "stock": 623927}},
            },
        }}

    def edit_common_box_detail(self, payload):
        edit = payload["editCommonCollectBoxDetail"]
        self.current = list(edit.get("sourceList") or self.current)
        self.edit_calls.append(self.current[0].get("sourceItemId") or "")
        # 上游每次返回新 ossMd5（实测确实如此）
        self._oss = "oss-%d" % (len(self.edit_calls))
        return {"result": "success", "data": {"ossMd5": self._oss}}

    def claim_common_to_platform(self, pairs):
        self._seq += 1
        new_id = 9000 + self._seq
        # 关键：把**当时**的链接快照存下来
        self.snapshots[new_id] = self.current[0]["sourceItemId"]
        return {"result": "success",
                "data": {"platformCollectBoxDetailIdMap": {
                    "tiktok": {str(pairs[0][0]): new_id}}}}


@pytest.fixture()
def fast_carrier(monkeypatch):
    """把载体重建里与网络/DB 相关的环节全部短路，只测链接分配。"""
    from services import carrier_service

    def fake_offer_id(url):
        from services.source_link import extract_offer_id
        return extract_offer_id(url)

    def fake_find_in_common_box(client, offer):
        return 4148378072

    monkeypatch.setattr(carrier_service, "offer_id", fake_offer_id)
    monkeypatch.setattr(carrier_service, "find_in_common_box", fake_find_in_common_box)
    monkeypatch.setattr(carrier_service, "take_link", lambda db: None)
    monkeypatch.setattr(carrier_service, "mark", lambda db, url, ok: None)
    monkeypatch.setattr(carrier_service, "_collect_box_snapshot",
                        lambda client, page_size=200: set())
    monkeypatch.setattr(carrier_service, "_retry_pause_seconds", lambda client: 0)
    return carrier_service


def test_every_copy_gets_its_own_link(fast_carrier):
    """一批 4 件 → 4 份副本，各自链接不同，且与 preferred 一一对应。"""
    client = LinkAwareClaimClient()
    preferred = [
        "https://detail.1688.com/offer/800000000001.html",
        "https://detail.1688.com/offer/800000000002.html",
        "https://detail.1688.com/offer/800000000003.html",
        "https://detail.1688.com/offer/800000000004.html",
    ]

    details = fast_carrier.collect_carriers(
        None, client, count=4, preferred_urls=preferred)

    assert len(details) == 4
    got = [client.snapshots[detail_id] for detail_id in details]
    assert got == ["800000000001", "800000000002",
                   "800000000003", "800000000004"], got
    # 每件都改了一次链接（不是只改一次）
    assert len(client.edit_calls) == 4


def test_does_not_reread_detail_for_every_item(fast_carrier):
    """链式复用 ossMd5：50 件只该读 1 次详情，不是 50 次。

    实测 read 0.47s / edit 0.9s，读 50 次会白白多花 23 秒。
    """
    client = LinkAwareClaimClient()
    preferred = ["https://detail.1688.com/offer/8000000000%02d.html" % index
                 for index in range(1, 11)]

    fast_carrier.collect_carriers(None, client, count=10, preferred_urls=preferred)

    assert client.detail_calls == 1, client.detail_calls
    assert len(client.edit_calls) == 10


def test_fewer_links_than_copies_keeps_last(fast_carrier):
    """链接比件数少时，多出的件沿用最后一条 —— 不能退化成载体自带那条。

    真实场景：一批 5 件但只有 2 件抓到了货源，剩下 3 件只能复用第 2 条，
    至少保证它们不是「载体自己那条不相干链接」。
    """
    client = LinkAwareClaimClient(start_offer="111111111111")
    preferred = [
        "https://detail.1688.com/offer/800000000001.html",
        "https://detail.1688.com/offer/800000000002.html",
    ]

    details = fast_carrier.collect_carriers(
        None, client, count=4, preferred_urls=preferred)

    got = [client.snapshots[detail_id] for detail_id in details]
    assert got == ["800000000001", "800000000002",
                   "800000000002", "800000000002"], got
    assert "111111111111" not in got


def test_no_preferred_leaves_carrier_link_alone(fast_carrier):
    """没有 preferred 时不该乱改公共箱（存量商品都没链接，这是常态）。

    这条路每天都会走：178 件存量商品全都没货源，发布时 preferred 为空，
    必须保证这种情况下**一次 edit 都不发**，不增加任何耗时。
    """
    client = LinkAwareClaimClient(start_offer="111111111111")

    details = fast_carrier.collect_carriers(None, client, count=3, preferred_urls=[])

    assert len(details) == 3
    assert client.edit_calls == []
    assert all(client.snapshots[d] == "111111111111" for d in details)


def test_stock_is_capped_once_and_reused(fast_carrier):
    """stock 必须夹住（623927 → 99999），且不能每件都夹一遍。

    上游原始库存 623927 会被整单拒绝：
    「editCommonCollectBoxDetail.stock 不能大于 99999」
    """
    from services.source_link import COMMON_BOX_STOCK_CAP, override_common_box_source

    client = LinkAwareClaimClient()
    cache: dict = {}

    override_common_box_source(client, 4148378072,
                               "https://detail.1688.com/offer/800000000001.html",
                               cache=cache)
    template = cache["edit"]
    assert template["stock"] == COMMON_BOX_STOCK_CAP
    for sku in template["skuMap"].values():
        assert sku["stock"] == COMMON_BOX_STOCK_CAP


def test_cache_reuses_oss_md5_across_calls(fast_carrier):
    """链式 edit：第二次用的 ossMd5 应该是第一次返回的那个。"""
    seen: list[str] = []

    class OssTracking(LinkAwareClaimClient):
        def edit_common_box_detail(self, payload):
            seen.append(payload["ossMd5"])
            return super().edit_common_box_detail(payload)

    client = OssTracking()
    cache: dict = {}

    from services.source_link import override_common_box_source
    override_common_box_source(client, 4148378072,
                               "https://detail.1688.com/offer/800000000001.html",
                               cache=cache)
    override_common_box_source(client, 4148378072,
                               "https://detail.1688.com/offer/800000000002.html",
                               cache=cache)

    assert seen[0] == "oss-0"        # 第一次用读到的
    assert seen[1] == "oss-1"        # 第二次用第一次返回的
    assert client.detail_calls == 1


def test_cache_invalidated_on_edit_failure(fast_carrier):
    """edit 失败说明 ossMd5 可能失效，必须丢缓存让下次重读。"""
    from services.miaoshou import MiaoshouError
    from services.source_link import override_common_box_source

    class FailingOnce(LinkAwareClaimClient):
        def __init__(self):
            super().__init__()
            self.failed = False

        def edit_common_box_detail(self, payload):
            if not self.failed:
                self.failed = True
                raise MiaoshouError("oss 过期")
            return super().edit_common_box_detail(payload)

    client = FailingOnce()
    cache: dict = {}

    first = override_common_box_source(
        client, 4148378072, "https://detail.1688.com/offer/800000000001.html",
        cache=cache)
    assert first["ok"] is False
    assert "edit" not in cache          # 缓存已丢

    second = override_common_box_source(
        client, 4148378072, "https://detail.1688.com/offer/800000000002.html",
        cache=cache)
    assert second["ok"] is True
    assert client.detail_calls == 2     # 第二次重新读了

def test_retry_round_redoes_link_for_that_item(fast_carrier):
    """重试的那一件会重新改一次链接（因为复制失败了，快照没成）。

    重试路径必须仍然带正确链接，否则「第一轮失败、第二轮成功」的那件
    会顶着上一条链接进店铺 —— 比不改更糟。
    """
    class FlakyOnce(LinkAwareClaimClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def claim_common_to_platform(self, pairs):
            self.calls += 1
            if self.calls == 2:                      # 第 2 件第一次复制失败
                from services.miaoshou import MiaoshouError
                raise MiaoshouError("网关繁忙，请稍后重试", kind="gateway")
            return super().claim_common_to_platform(pairs)

    client = FlakyOnce()
    preferred = ["https://detail.1688.com/offer/8000000000%02d.html" % index
                 for index in range(1, 4)]

    details = fast_carrier.collect_carriers(None, client, count=3,
                                            preferred_urls=preferred)

    assert len(details) == 3
    # 3 件成品，但第 2 件复制了两次 → edit 至少 4 次
    assert len(client.edit_calls) >= 4
    # 每件成品对应的链接仍在 preferred 范围内，不能是载体自带那条
    for detail_id in details:
        assert client.snapshots[detail_id] in {"800000000001",
                                              "800000000002",
                                              "800000000003"}
