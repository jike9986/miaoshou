"""采集箱翻页不应该重复打上游（2026-11-27）。

负责人口径：「不管是从别的功能切换到自动化处理还是什么，
              这个采集箱要等很久才会加载出来」。

根因两个，叠在一起：

  ① 后端：缓存键带 page / page_size，**每页各打一次妙手**；而取第 N 页
     必须从上游第 1 页重扫。实测 pageSize=20 第 1 页 1.26s、第 2 页 2.7s，
     pageSize=100 第 2 页 9.9s —— 页越深越慢。
  ② 前端：`refetchOnMount: "always"`，每次切回本页都强制重拉一遍。

这里锁后端：同一组筛选条件下，翻多少页都只打一次上游。
"""
from __future__ import annotations

import pytest

from models.account import Account
from services import collect_box_service


class CountingClient:
    """记录打了几次上游，以及每次的页码。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[int] = []
        self.closed = False

    def collect_box_list(self, *, page_no: int, page_size: int, **_kwargs) -> dict:
        self.calls.append(page_no)
        start = (page_no - 1) * page_size
        return {"data": {"total": len(self.rows),
                         "detailList": self.rows[start:start + page_size]}}

    def close(self) -> None:
        self.closed = True


def row(detail_id: int, shop: int = 1) -> dict:
    return {"collectBoxDetailId": detail_id, "title": f"Product {detail_id}",
            "site": "US", "editModel": "shop",
            "collectBoxDetailShopList": [{"shopId": shop}]}


@pytest.fixture()
def service(monkeypatch):
    """清掉缓存，避免用例之间互相命中。"""
    collect_box_service._cache().clear()
    return collect_box_service


class EmptyDb:
    """没有任何仓库商品的库 —— 这几个用例只关心翻页与缓存，不看入仓状态。"""

    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return []


def _account() -> Account:
    return Account(id=1, app_key="k", app_secret_enc="s")


def test_paging_does_not_rescan_upstream(service, monkeypatch):
    """page=1 → page=2 → page=3 只应该打一次上游（第二次起走缓存）。

    原来每个 page 各占一个缓存条目，翻三页就打三遍上游。
    """
    client = CountingClient([row(i) for i in range(1, 241)])
    monkeypatch.setattr(service, "account_client", lambda _a: client)

    first = service.list_items(EmptyDb(), _account(), page=1, page_size=20,
                               status="notPublished")
    calls_after_first = len(client.calls)
    second = service.list_items(EmptyDb(), _account(), page=2, page_size=20,
                                status="notPublished")
    third = service.list_items(EmptyDb(), _account(), page=3, page_size=20,
                               status="notPublished")

    assert len(client.calls) == calls_after_first, "翻页不该重新扫上游"
    assert second["cached"] and third["cached"], "第二页起应命中快照"

    # 各页内容不重叠、顺序连续
    assert [i["detailId"] for i in first["items"]][:3] == ["1", "2", "3"]
    assert [i["detailId"] for i in second["items"]][:3] == ["21", "22", "23"]
    assert [i["detailId"] for i in third["items"]][:3] == ["41", "42", "43"]
    assert first["hasMore"] and second["hasMore"] and third["hasMore"]


def test_page_size_change_reuses_same_snapshot(service, monkeypatch):
    """换每页条数也只是切片，不重新打上游 —— 用户改 20/50/100 立刻出结果。"""
    client = CountingClient([row(i) for i in range(1, 121)])
    monkeypatch.setattr(service, "account_client", lambda _a: client)

    service.list_items(EmptyDb(), _account(), page=1, page_size=20,
                       status="notPublished")
    before = len(client.calls)
    wide = service.list_items(EmptyDb(), _account(), page=1, page_size=50,
                              status="notPublished")

    assert len(client.calls) == before, "改页长不该重新扫上游"
    assert len(wide["items"]) == 50


def test_last_page_has_no_more(service, monkeypatch):
    """最后一页 hasMore 为假，前端才知道到底了。"""
    client = CountingClient([row(i) for i in range(1, 41)])
    monkeypatch.setattr(service, "account_client", lambda _a: client)

    last = service.list_items(EmptyDb(), _account(), page=2, page_size=20,
                              status="notPublished")
    assert len(last["items"]) == 20
    assert last["hasMore"] is False
    assert last["total"] == 40


def test_saved_desc_sort_applies_across_pages(service, monkeypatch):
    """按仓库状态排序要按全量排，不能只排当前页。

    `_annotate_warehouse` 必须在切片之前跑，否则第 2 页里已入仓的商品
    排不到前面 —— 那正是运营找「哪些已经处理过」时最需要看到的顺序。
    """
    client = CountingClient([row(i) for i in range(1, 41)])
    monkeypatch.setattr(service, "account_client", lambda _a: client)

    class FakeProduct:
        def __init__(self, key: str) -> None:
            self.warehouse_key = key
            self.id = 1
            self.collected: dict = {}

    class FakeQuery:
        def __init__(self, keys: set[str]) -> None:
            self.keys = keys

        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return [FakeProduct(k) for k in self.keys]

    class FakeDb:
        def query(self, *_args, **_kwargs):
            # 只有第 35 件在仓库里（key 格式是 {detailId}:{shopId}）
            return FakeQuery({"35:1"})

    page1 = service.list_items(FakeDb(), _account(), page=1, page_size=20,
                               status="notPublished", sort="savedDesc")
    page2 = service.list_items(FakeDb(), _account(), page=2, page_size=20,
                               status="notPublished", sort="savedDesc")

    # 已入仓的那件必须排在整份列表最前面 = 第 1 页第 1 位
    assert page1["items"][0]["detailId"] == "35"
    assert page1["items"][0]["inWarehouse"] is True