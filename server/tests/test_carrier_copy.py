import pytest

from services import carrier_service
from services.miaoshou import MiaoshouError


class ExistingCommonBox:
    def __init__(self):
        self.calls = []
        self.box_ids = []

    def common_box_list(self, **kwargs):
        return {"data": {"detailList": [{"status": "success", "commonCollectBoxDetailId": 42}]}}

    def claim_common_to_platform(self, items):
        self.calls.append(items)
        count = items[0][2]
        ids = list(range(1000 + (len(self.calls) - 1) * 100,
                         1000 + (len(self.calls) - 1) * 100 + count))
        self.box_ids.extend(ids)
        return {"data": {"detailIds": ids}}

    def collect_box_list(self, **kwargs):
        return {"data": {"detailList": [
            {"collectBoxDetailId": value, "commonCollectBoxDetailId": 42}
            for value in self.box_ids
        ]}}


def test_existing_common_box_copies_in_batches(monkeypatch):
    client = ExistingCommonBox()
    monkeypatch.setattr(
        carrier_service, "take_link", lambda *_: pytest.fail("should reuse common box"))
    ids = carrier_service.collect_carriers(None, client, count=137)
    assert len(ids) == len(set(ids)) == 137
    assert all(call[0][0] == 42 for call in client.calls)
    assert [call[0][2] for call in client.calls] == [100, 37]


def test_partial_copy_receipt_recovers_all_new_drafts_from_box():
    client = ExistingCommonBox()
    def partial(items):
        client.calls.append(items)
        count = items[0][2]
        client.box_ids.extend(range(1001, 1001 + count))
        return {"data": {"detailIds": [1001]}}
    client.claim_common_to_platform = partial
    ids = carrier_service.collect_carriers(None, client, count=20)
    assert ids == list(range(1001, 1021))
    assert len(client.calls) == 1


def test_readback_skips_existing_same_source_rows_before_scanning_new_copies():
    client = ExistingCommonBox()
    client.box_ids = list(range(900, 925))

    def partial(items):
        client.calls.append(items)
        client.box_ids.extend([2001, 2002, 2003])
        return {"data": {"detailIds": [2001]}}

    client.claim_common_to_platform = partial
    ids = carrier_service.collect_carriers(None, client, count=3)
    assert ids == [2001, 2002, 2003]
    assert len(client.calls) == 1


def test_partial_copy_receipt_without_full_new_drafts_stops_without_retry():
    client = ExistingCommonBox()
    def partial(items):
        client.calls.append(items)
        return {"data": {"detailIds": [1001]}}
    client.claim_common_to_platform = partial
    with pytest.raises(carrier_service.CarrierError, match="数量不完整"):
        carrier_service.collect_carriers(None, client, count=20)
    assert len(client.calls) == 1


def test_copy_receipt_deduplicates_ids_across_fields():
    client = ExistingCommonBox()
    client.claim_common_to_platform = lambda *_: {"data": {
        "detailIds": [1001, 1002], "collectBoxDetailIds": [1001, 1002],
    }}
    assert carrier_service.collect_carriers(None, client, count=2) == [1001, 1002]


def test_missing_copy_ids_stops_without_using_existing_products():
    client = ExistingCommonBox()
    client.claim_common_to_platform = lambda *_: {"data": {}}
    with pytest.raises(carrier_service.CarrierError, match="勿重复操作"):
        carrier_service.collect_carriers(None, client, count=1)


def test_uncertain_copy_failure_is_not_retried():
    client = ExistingCommonBox()
    def fail(items):
        client.calls.append(items)
        raise MiaoshouError("timeout", kind="error")
    client.claim_common_to_platform = fail
    with pytest.raises(carrier_service.CarrierError, match="勿重复操作"):
        carrier_service.collect_carriers(None, client, count=1)
    assert len(client.calls) == 1
