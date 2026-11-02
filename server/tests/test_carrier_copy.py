import pytest

from services import carrier_service
from services.miaoshou import MiaoshouError


class ExistingCommonBox:
    def __init__(self):
        self.calls = []

    def common_box_list(self, **kwargs):
        return {"data": {"detailList": [{"status": "success", "commonCollectBoxDetailId": 42}]}}

    def claim_common_to_platform(self, items):
        self.calls.append(items)
        count = items[0][2]
        return {"data": {"detailIds": list(range(1000 + (len(self.calls) - 1) * 100,
                                                  1000 + (len(self.calls) - 1) * 100 + count))}}


def test_existing_common_box_copies_in_batches(monkeypatch):
    client = ExistingCommonBox()
    monkeypatch.setattr(carrier_service, "take_link", lambda *_: pytest.fail("should reuse common box"))
    ids = carrier_service.collect_carriers(None, client, count=137)
    assert len(ids) == len(set(ids)) == 137
    assert all(call[0][0] == 42 for call in client.calls)
    assert [call[0][2] for call in client.calls] == [100, 37]


def test_partial_copy_receipt_stops_without_retry():
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
