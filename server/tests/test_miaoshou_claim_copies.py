import pytest

from services.miaoshou import COMMON_BOX_BASE, MiaoshouClient, MiaoshouError


def test_claim_common_expands_copy_count_into_serial_number_rows(monkeypatch):
    client = MiaoshouClient.__new__(MiaoshouClient)
    calls = []

    def post(path, body):
        calls.append((path, body))
        return {"data": {"detailIds": [101, 102, 103]}}

    monkeypatch.setattr(client, "post", post)
    response = client.claim_common_to_platform([(42, "tiktok", 3)])

    assert response["data"]["detailIds"] == [101, 102, 103]
    assert calls == [(
        COMMON_BOX_BASE + "claimed",
        {"detailSerialNumberPlatformList": [
            {"detailId": 42, "platform": "tiktok", "serialNumber": 1},
            {"detailId": 42, "platform": "tiktok", "serialNumber": 2},
            {"detailId": 42, "platform": "tiktok", "serialNumber": 3},
        ]},
    )]


def test_claim_common_rejects_more_than_api_batch_limit():
    client = MiaoshouClient.__new__(MiaoshouClient)
    with pytest.raises(MiaoshouError, match="最多认领 100"):
        client.claim_common_to_platform([(42, "tiktok", 101)])
