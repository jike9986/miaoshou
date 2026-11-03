import pytest

from services.miaoshou import COMMON_BOX_BASE, MiaoshouClient, MiaoshouError


def test_claim_common_keeps_single_copy_serial_number(monkeypatch):
    client = MiaoshouClient.__new__(MiaoshouClient)
    calls = []

    def post(path, body):
        calls.append((path, body))
        return {"data": {"detailIds": [101]}}

    monkeypatch.setattr(client, "post", post)
    response = client.claim_common_to_platform([(42, "tiktok", 1)])

    assert response["data"]["detailIds"] == [101]
    assert calls == [(
        COMMON_BOX_BASE + "claimed",
        {"detailSerialNumberPlatformList": [
            {"detailId": 42, "platform": "tiktok", "serialNumber": 1},
        ]},
    )]


def test_claim_common_rejects_more_than_api_batch_limit():
    client = MiaoshouClient.__new__(MiaoshouClient)
    with pytest.raises(MiaoshouError, match="最多认领 100"):
        client.claim_common_to_platform([(index, "tiktok", 1) for index in range(101)])
