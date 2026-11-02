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
        return {"data": {"detailIds": [1000 + len(self.calls)]}}


def test_existing_common_box_can_create_more_than_eight_copies(monkeypatch):
    client = ExistingCommonBox()
    monkeypatch.setattr(carrier_service, "take_link", lambda *_: pytest.fail("should reuse common box"))
    ids = carrier_service.collect_carriers(None, client, count=137)
    assert len(ids) == len(set(ids)) == 137
    assert all(call[0][0] == 42 for call in client.calls)


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
