from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from core import errors
from services.warehouse_service import _save_processed_draft


class DraftClient:
    def __init__(self, readback: dict[str, Any] | None = None) -> None:
        self.saved: dict[str, Any] | None = None
        self.readback = readback

    def save_shop(self, detail_id: str, shop_id: str, oss_md5: str,
                  item: dict[str, Any]) -> None:
        self.saved = deepcopy(item)

    def shop_detail(self, detail_id: str, shop_id: str) -> dict[str, Any]:
        item = self.readback if self.readback is not None else self.saved
        if item is None:
            return {"data": {"shopCollectItemInfo": {}}}
        return {"data": {"shopCollectItemInfo": deepcopy(item)}}


def _product(title: str) -> dict[str, Any]:
    return {
        "title": title,
        "cid": "100",
        "weight": "0.5",
        "packageLength": 30,
        "packageWidth": 20,
        "packageHeight": 10,
        "sizeChart": "",
        "imgUrls": [],
        "notes": "",
        "skuPropertyList": [],
        "skuMap": {},
    }


def test_processed_draft_requires_matching_miaoshou_readback() -> None:
    original = _product("Original product title")
    processed = _product("Processed product title")
    client = DraftClient()

    _save_processed_draft(
        client, detail_id="123", shop_id="456", oss_md5="version-1",
        payload=processed, original=original, coverage={}, pricing_enabled=False,
    )

    assert client.saved == processed


def test_processed_draft_blocks_mismatched_readback() -> None:
    original = _product("Original product title")
    processed = _product("Processed product title")
    client = DraftClient(readback=_product("Different title"))

    with pytest.raises(errors.AppError) as raised:
        _save_processed_draft(
            client, detail_id="123", shop_id="456", oss_md5="version-1",
            payload=processed, original=original, coverage={}, pricing_enabled=False,
        )

    assert raised.value.status == 502
    assert raised.value.details["mismatches"] == ["title"]
    assert client.saved == processed


def test_processed_draft_blocks_unreadable_result() -> None:
    original = _product("Original product title")
    processed = _product("Processed product title")
    client = DraftClient(readback={})

    with pytest.raises(errors.AppError) as raised:
        _save_processed_draft(
            client, detail_id="123", shop_id="456", oss_md5="version-1",
            payload=processed, original=original, coverage={}, pricing_enabled=False,
        )

    assert raised.value.status == 502


def test_processed_draft_blocks_when_template_prices_were_not_saved() -> None:
    original = _product("Original product title")
    original.update({
        "price": "10.00",
        "originPrice": "5.00",
        "skuMap": {"sku-1": {"price": "10.00", "originPrice": "5.00"}},
    })
    processed = deepcopy(original)
    processed.update({
        "title": "Processed product title",
        "price": "18.50",
        "skuMap": {"sku-1": {"price": "18.50", "originPrice": "5.00"}},
    })
    readback = deepcopy(processed)
    readback["price"] = "10.00"
    readback["skuMap"]["sku-1"]["price"] = "10.00"

    with pytest.raises(errors.AppError) as raised:
        _save_processed_draft(
            DraftClient(readback=readback),
            detail_id="123", shop_id="456", oss_md5="version-1",
            payload=processed, original=original, coverage={}, pricing_enabled=True,
        )

    assert raised.value.status == 502
    assert raised.value.details["mismatches"] == ["prices"]
