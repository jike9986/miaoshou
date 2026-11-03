from __future__ import annotations

import pytest

from models.account import Account
from services import collect_box_service


class CollectBoxStub:
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


def row(detail_id: int, *, shops: tuple[int, ...] = (1,), site: str = "US") -> dict:
    return {"collectBoxDetailId": detail_id, "title": f"Product {detail_id}",
            "site": site, "editModel": "shop",
            "collectBoxDetailShopList": [{"shopId": shop_id} for shop_id in shops]}


def scan(monkeypatch, rows: list[dict], *, page: int, page_size: int,
         shop_id: str | None = None, site: str | None = None) -> tuple[dict, CollectBoxStub]:
    client = CollectBoxStub(rows)
    monkeypatch.setattr(collect_box_service, "account_client", lambda _account: client)
    result = collect_box_service._scan_upstream(
        Account(id=1), page=page, page_size=page_size, status="notPublished",
        keyword="", shop_id=shop_id, site=site)
    assert client.closed
    return result, client


@pytest.mark.parametrize("page,page_size", [(2, 20), (2, 50), (2, 100), (11, 20)])
def test_logical_pages_do_not_overlap_or_stop_after_first_page(
        monkeypatch, page: int, page_size: int) -> None:
    result, client = scan(monkeypatch, [row(i) for i in range(1, 242)],
                          page=page, page_size=page_size)
    start = (page - 1) * page_size + 1
    assert [item["detailId"] for item in result["items"]] == [
        str(i) for i in range(start, start + page_size)]
    assert result["hasMore"]
    assert client.calls == list(range(1, client.calls[-1] + 1))


def test_filtering_counts_matching_shop_rows_before_slicing(monkeypatch) -> None:
    rows = [row(i, shops=(1, 2) if i % 5 == 0 else (1,),
                site="GB" if i % 10 == 0 else "US") for i in range(1, 96)]
    result, _client = scan(monkeypatch, rows, page=2, page_size=3,
                           shop_id="2", site="gb")
    assert [item["detailId"] for item in result["items"]] == ["40", "50", "60"]
    assert all(item["shopId"] == "2" for item in result["items"])
    assert result["hasMore"]


def test_multiple_shop_rows_use_logical_page_boundaries(monkeypatch) -> None:
    result, _client = scan(monkeypatch, [row(i, shops=(1, 2)) for i in range(1, 31)],
                           page=2, page_size=25)
    expected = [(str(i), str(shop)) for i in range(1, 31) for shop in (1, 2)][25:50]
    assert [(item["detailId"], item["shopId"]) for item in result["items"]] == expected
    assert result["hasMore"]


@pytest.mark.parametrize("count,expected_count", [(40, 20), (35, 15), (20, 0)])
def test_last_page_reports_no_more_items(monkeypatch, count: int, expected_count: int) -> None:
    result, client = scan(monkeypatch, [row(i) for i in range(1, count + 1)],
                          page=2, page_size=20)
    assert len(result["items"]) == expected_count
    assert not result["hasMore"]
    assert len(client.calls) <= 2


def test_verify_collect_box_readback_retries_short_visibility_delay(monkeypatch) -> None:
    class DelayedClient:
        def __init__(self) -> None:
            self.calls = 0

        def collect_box_list(self, **_kwargs) -> dict:
            self.calls += 1
            rows = [] if self.calls == 1 else [row(321, shops=(9,))]
            return {"data": {"total": len(rows), "detailList": rows}}

    client = DelayedClient()
    sleeps: list[float] = []
    monkeypatch.setattr(collect_box_service.time, "sleep", sleeps.append)

    assert collect_box_service.verify_items_present(
        client, ["321"], "9", retries=2, retry_delay=0.05) == {"321"}
    assert client.calls == 2
    assert sleeps == [0.05]


def test_verify_collect_box_readback_requires_target_shop() -> None:
    client = CollectBoxStub([row(321, shops=(8,))])
    assert collect_box_service.verify_items_present(
        client, ["321"], "9", retries=0) == set()
