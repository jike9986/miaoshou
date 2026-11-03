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
        assert items == [(42, "tiktok", 1)]
        ids = [1000 + len(self.calls) - 1]
        self.box_ids.extend(ids)
        return {"data": {"platformCollectBoxDetailIdMap": {
            "tiktok": {"42": ids[0]},
        }}}

    def collect_box_list(self, **kwargs):
        return {"data": {"detailList": [
            {"collectBoxDetailId": value, "commonCollectBoxDetailId": 42}
            for value in self.box_ids
        ]}}


def test_existing_common_box_copies_one_at_a_time_with_spacing(monkeypatch):
    client = ExistingCommonBox()
    sleeps = []
    monkeypatch.setattr(
        carrier_service, "take_link", lambda *_: pytest.fail("should reuse common box"))
    monkeypatch.setattr(carrier_service.time, "sleep", sleeps.append)
    ids = carrier_service.collect_carriers(None, client, count=137)
    assert len(ids) == len(set(ids)) == 137
    assert all(call[0][0] == 42 for call in client.calls)
    assert all(call[0][2] == 1 for call in client.calls)
    assert len(client.calls) == 137
    assert sleeps == [0.7] * 135


def test_missing_single_copy_receipt_recovers_new_draft_from_box():
    client = ExistingCommonBox()
    def partial(items):
        client.calls.append(items)
        client.box_ids.append(1001)
        return {"data": {}}
    client.claim_common_to_platform = partial
    ids = carrier_service.collect_carriers(None, client, count=1)
    assert ids == [1001]
    assert len(client.calls) == 1


def test_readback_skips_existing_same_source_rows_before_scanning_new_copies():
    client = ExistingCommonBox()
    client.box_ids = list(range(900, 925))

    def partial(items):
        client.calls.append(items)
        detail_id = 2000 + len(client.calls)
        client.box_ids.append(detail_id)
        return {"data": {"platformCollectBoxDetailIdMap": {
            "tiktok": {"42": detail_id},
        }}}

    client.claim_common_to_platform = partial
    ids = carrier_service.collect_carriers(None, client, count=3)
    assert ids == [2001, 2002, 2003]
    assert len(client.calls) == 3


def test_partial_copy_receipt_without_full_new_drafts_stops_without_retry():
    client = ExistingCommonBox()
    def partial_then_fail(items):
        client.calls.append(items)
        if len(client.calls) == 1:
            client.box_ids.append(1001)
            return {"data": {"detailIds": [1001]}}
        raise MiaoshouError("rate limited", kind="error")
    client.claim_common_to_platform = partial_then_fail
    with pytest.raises(carrier_service.CarrierError, match="结果未确认"):
        carrier_service.collect_carriers(None, client, count=2)
    assert len(client.calls) == 2


def test_wait_until_collected_uses_public_box_list_status():
    class CollectingCommonBox(ExistingCommonBox):
        def common_box_list(self, **kwargs):
            return {"data": {"detailList": [
                {"commonCollectBoxDetailId": 42, "status": "success"},
            ]}}

        def common_box_detail(self, *_args):
            pytest.fail("stable collection status must come from the list")

    assert carrier_service.wait_until_collected(CollectingCommonBox(), 42)


def test_copy_receipt_deduplicates_ids_across_fields():
    client = ExistingCommonBox()
    client.claim_common_to_platform = lambda *_: {"data": {
        "platformCollectBoxDetailIdMap": {"tiktok": {"42": 1001}},
        "detailIds": [1001],
    }}
    assert carrier_service.collect_carriers(None, client, count=1) == [1001]


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
