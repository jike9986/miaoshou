import threading
import time

import httpx
import pytest

from services import carrier_service, publish_service
from services.miaoshou import MiaoshouClient, MiaoshouError


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


def test_existing_common_box_copies_one_at_a_time_without_redundant_sleep(monkeypatch):
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
    assert sleeps == []


def test_fill_can_skip_readback_for_claim_only_workflow(monkeypatch):
    class Client:
        def __init__(self):
            self.details = []
            self.saves = []

        def warehouse_list(self, _shop_ids):
            return {"data": {"shopWarehouseList": [{"shopId": 9,
                "warehouseList": [{"isDefault": "1", "warehouseId": "w1"}]}]}}

        def shop_detail(self, detail_id, shop_id):
            self.details.append((detail_id, shop_id))
            return {"data": {"ossMd5": "version"}}

        def save_shop(self, detail_id, shop_id, oss_md5, body):
            self.saves.append((detail_id, shop_id, oss_md5, body))

    client = Client()
    monkeypatch.setattr(carrier_service, "_readback_mismatches",
                        lambda *_args: pytest.fail("claim-only flow must skip readback"))
    result = carrier_service.fill_carriers(
        client, [1001], [{"title": "finished", "cid": ""}], shop_id=9,
        verify_readback=False)

    assert result["saved"] == [1001]
    assert client.details == [(1001, 9)]
    assert len(client.saves) == 1


def test_collect_and_fill_reports_cumulative_item_progress(monkeypatch):
    def collect(_db, _client, *, count, progress, on_carrier):
        for detail_id in range(100, 100 + count):
            on_carrier(detail_id)

    def fill(_client, carriers, _payloads, *, progress, **_kwargs):
        detail_id = carriers[0]
        progress({"stage": "fill", "detailId": detail_id})
        if detail_id == 100:
            return {"saved": [detail_id], "savedSources": [0], "failures": [],
                    "failureDetails": [], "priceAdjustments": []}
        return {"saved": [], "savedSources": [], "failures": ["尺寸缺失"],
                "failureDetails": [{"detailId": str(detail_id), "certain": True,
                                    "reason": "尺寸缺失"}],
                "priceAdjustments": []}

    monkeypatch.setattr(carrier_service, "target_warehouse_id", lambda *_: "w1")
    monkeypatch.setattr(carrier_service, "collect_carriers", collect)
    monkeypatch.setattr(carrier_service, "fill_carriers", fill)
    events = []

    result = carrier_service.collect_and_fill_carriers(
        None, object(), payloads=[{}, {}], shop_id=9, progress=events.append)

    completed = [event for event in events
                 if event.get("stage") in {"verified", "verify_failed"}]
    assert [(event["processed"], event["verified"], event["failed"])
            for event in completed] == [(1, 1, 0), (2, 1, 1)]
    assert [event["sourceIndex"] for event in completed] == [0, 1]
    assert result["saved"] == [100]
    assert result["failedSources"] == [1]


def test_claim_image_limits_apply_to_copy_without_mutating_warehouse_payload():
    from services.pre_save_check import extract_detail_image_urls

    detail_urls = [f"https://img.example/{index}.jpg" for index in range(35)]
    main_urls = [f"https://main.example/{index}.jpg" for index in range(9)]
    main_urls.extend([main_urls[-1], "https://main.example/9.jpg",
                      "https://main.example/10.jpg"])
    original = {"imgUrls": main_urls,
                "notes": ("<div>Product description</div>"
                          + f'<img src="{detail_urls[0]}">'
                          + "".join(f'<img src="{url}">' for url in detail_urls)),
                "skuMap": {"sku-a": {"price": 9.19}}}

    [claim_payload] = publish_service._limit_claim_images([original])

    assert len(claim_payload["imgUrls"]) == 9
    assert len(set(claim_payload["imgUrls"])) == 9
    assert len(extract_detail_image_urls(claim_payload["notes"])) == 30
    assert "Product description" in claim_payload["notes"]
    assert "34.jpg" not in claim_payload["notes"]
    assert len(set(extract_detail_image_urls(claim_payload["notes"]))) == 30
    assert len(original["imgUrls"]) == 12
    assert len(extract_detail_image_urls(original["notes"])) == 36
    assert claim_payload["skuMap"] == original["skuMap"]


def test_claim_image_limit_discards_blank_main_image_entries():
    payload = {"imgUrls": ["", " ", "a.jpg", "a.jpg", *[
        f"{index}.jpg" for index in range(1, 10)]]}

    [limited] = publish_service._limit_claim_images([payload])

    assert limited["imgUrls"] == ["a.jpg", *[f"{index}.jpg" for index in range(1, 9)]]


@pytest.mark.parametrize(("category", "expected"), [
    ("运动鞋", (33.0, 22.0, 12.0)),
    ("轮滑鞋与溜冰鞋", (35.0, 24.0, 14.0)),
    ("运动服饰", (30.0, 24.0, 6.0)),
])
def test_claim_uses_unverified_category_package_estimates_on_copy(
        category, expected):
    original = {"categoryName": category}
    [claim_payload] = publish_service._limit_claim_images([original])

    adjustments = publish_service._fill_claim_package_dimensions(
        [claim_payload], [{"warehouseId": "20"}])

    assert tuple(claim_payload[key] for key in (
        "packageLength", "packageWidth", "packageHeight")) == expected
    assert original == {"categoryName": category}
    assert adjustments == [{
        "warehouseId": "20",
        "stage": "package_dimensions",
        "source": "category_default_estimate",
        "verified": False,
        "packageDimensions": {
            "packageLength": expected[0],
            "packageWidth": expected[1],
            "packageHeight": expected[2],
        },
    }]


def test_claim_package_estimate_preserves_existing_measurements():
    payload = {"categoryName": "运动鞋", "packageLength": 40,
               "packageWidth": 21, "packageHeight": None}

    publish_service._fill_claim_package_dimensions(
        [payload], [{"warehouseId": "20"}])

    assert (payload["packageLength"], payload["packageWidth"],
            payload["packageHeight"]) == (40, 21, 12.0)


def test_claim_uses_consistent_sku_weight_only_for_missing_product_weight():
    payload = {"weight": None, "skuMap": {
        "sku-a": {"weight": 0.8, "isDelete": "0"},
        "sku-b": {"weight": "0.8", "isDelete": "0"},
    }}

    adapted = carrier_service.adapt_payload(
        payload, shop_id=9, warehouse_id="w1")

    assert adapted["weight"] == 0.8
    assert "packageLength" not in adapted
    assert "packageWidth" not in adapted
    assert "packageHeight" not in adapted
    assert payload["weight"] is None


def test_claim_defaults_missing_cod_switch_and_preserves_explicit_value():
    missing = {"isCodOpen": None}
    explicit = {"isCodOpen": "0"}

    adapted_missing = carrier_service.adapt_payload(
        missing, shop_id=9, warehouse_id="w1")
    adapted_explicit = carrier_service.adapt_payload(
        explicit, shop_id=9, warehouse_id="w1")

    assert adapted_missing["isCodOpen"] == "1"
    assert adapted_explicit["isCodOpen"] == "0"
    assert missing["isCodOpen"] is None


def test_claim_does_not_infer_product_weight_from_inconsistent_skus():
    payload = {"weight": None, "skuMap": {
        "sku-a": {"weight": 0.8, "isDelete": "0"},
        "sku-b": {"weight": 0.9, "isDelete": "0"},
    }}

    adapted = carrier_service.adapt_payload(
        payload, shop_id=9, warehouse_id="w1")

    assert adapted["weight"] is None


def test_claim_description_uses_existing_title_when_notes_are_image_only():
    payload = {"title": "Plain & simple", "notes": "<p> </p><img src='a.jpg'>"}

    changed = publish_service._ensure_claim_description(payload)

    assert changed is True
    assert payload["notes"].startswith("<p>Plain &amp; simple</p>")
    assert "<img src='a.jpg'>" in payload["notes"]


def test_claim_deduplicates_repeated_color_only_when_sku_rows_match_except_item_num():
    payload = {"skuPropertyList": [{"attrName": "warna", "attrValueList": [
        {"attrValueId": "white-a", "attrValue": "Putih"},
        {"attrValueId": "white-b", "attrValue": " putih "},
    ]}], "skuMap": {
        "white-a;size-s": {"price": "10.00", "stock": 4, "itemNum": "A-S"},
        "white-a;size-m": {"price": "12.00", "stock": 3, "itemNum": "A-M"},
        "white-b;size-s": {"price": "10.00", "stock": 4, "itemNum": "B-S"},
        "white-b;size-m": {"price": "12.00", "stock": 3, "itemNum": "B-M"},
    }}

    result = publish_service._dedupe_exact_duplicate_claim_colors(payload)

    assert result == {"removedColorValues": 1, "removedSkuVariants": 2}
    assert [row["attrValueId"] for row in payload["skuPropertyList"][0]["attrValueList"]] == [
        "white-a"]
    assert set(payload["skuMap"]) == {"white-a;size-s", "white-a;size-m"}


def test_claim_keeps_repeated_color_when_sku_data_differs():
    payload = {"skuPropertyList": [{"attrName": "warna", "attrValueList": [
        {"attrValueId": "white-a", "attrValue": "Putih"},
        {"attrValueId": "white-b", "attrValue": "Putih"},
    ]}], "skuMap": {
        "white-a;size-s": {"price": "10.00", "stock": 4, "itemNum": "A-S"},
        "white-b;size-s": {"price": "10.00", "stock": 2, "itemNum": "B-S"},
    }}

    result = publish_service._dedupe_exact_duplicate_claim_colors(payload)

    assert result is None
    assert len(payload["skuPropertyList"][0]["attrValueList"]) == 2
    assert set(payload["skuMap"]) == {"white-a;size-s", "white-b;size-s"}


def test_claim_renames_duplicate_long_colors_without_exceeding_platform_limit():
    long_color = "Color " + "x" * 50
    payload = {"skuPropertyList": [{"attrName": "Warna", "attrValueList": [
        {"attrValueId": "color-a", "attrValue": long_color},
        {"attrValueId": "color-b", "attrValue": long_color},
    ]}]}

    changed = publish_service._disambiguate_duplicate_claim_colors(payload)
    names = [row["attrValue"] for row in payload["skuPropertyList"][0]["attrValueList"]]

    assert changed == 2
    assert all(len(name) <= 50 for name in names)
    assert len({name.casefold() for name in names}) == 2


def test_claim_trims_only_size_recommendation_and_keeps_variants_unique():
    payload = {"skuPropertyList": [{"attrName": "Saiz", "attrValueList": [
        {"attrValueId": "size-a", "attrValue": "36-37"},
        {"attrValueId": "size-b", "attrValue": (
            "36-37 (Kami Mengesyorkan Anda Memesan Saiz yang Lebih Besar daripada biasa)"
        )},
        {"attrValueId": "size-c", "attrValue": "Custom " + "x" * 55},
    ]}]}

    changed = publish_service._trim_long_claim_size_values(payload)
    names = [row["attrValue"] for row in payload["skuPropertyList"][0]["attrValueList"]]

    assert changed == 1
    assert names == ["36-37", "36-37 (2)", "Custom " + "x" * 55]


def test_claim_maps_known_season_values_to_miaoshou_ids():
    payload = {"productAttributes": [
        {"attributeId": "100397", "attributeValues": [
            {"valueName": "Musim Bunga"}, {"valueName": "Musim Panas"},
        ]},
        {"attributeId": "other", "attributeValues": [
            {"valueName": "Musim Bunga"},
        ]},
    ]}

    adapted = carrier_service.adapt_payload(
        payload, shop_id=9, warehouse_id="w1")

    values = adapted["productAttributes"][0]["attributeValues"]
    assert [value["valueId"] for value in values] == ["1001161", "1001162"]
    assert "valueId" not in adapted["productAttributes"][1]["attributeValues"][0]
    assert "valueId" not in payload["productAttributes"][0]["attributeValues"][0]


def test_claim_keeps_repeated_color_when_color_images_differ():
    payload = {"skuPropertyList": [{"attrName": "warna", "attrValueList": [
        {"attrValueId": "white-a", "attrValue": "Putih", "imgUrl": "a.jpg"},
        {"attrValueId": "white-b", "attrValue": " putih ", "imgUrl": "b.jpg"},
    ]}], "skuMap": {
        "white-a;size-s": {"price": "10.00", "stock": 4, "itemNum": "A-S"},
        "white-b;size-s": {"price": "10.00", "stock": 4, "itemNum": "B-S"},
    }}

    result = publish_service._dedupe_exact_duplicate_claim_colors(payload)

    assert result is None
    renamed = publish_service._disambiguate_duplicate_claim_colors(payload)
    assert renamed == 1
    assert payload["skuPropertyList"][0]["attrValueList"] == [
        {"attrValueId": "white-a", "attrValue": "Putih", "imgUrl": "a.jpg"},
        {"attrValueId": "white-b", "attrValue": "putih (2)", "imgUrl": "b.jpg"},
    ]
    assert set(payload["skuMap"]) == {"white-a;size-s", "white-b;size-s"}


def test_reported_cost_floor_raises_only_active_matching_skus_once():
    message = ("产品设置的本地展示价9.19MYR，无法覆盖产品基础成本"
               "(跨境物流运费+关税+增值税)：9.66MYR")
    error = MiaoshouError(message, payload={"message": message})
    payload = {"skuMap": {
        "active": {"price": "9.19", "priceIncludeVat": "9.19",
                   "originPrice": "2.40", "isDelete": "0"},
        "other": {"price": "12.00", "priceIncludeVat": "12.00"},
        "deleted": {"price": "9.19", "priceIncludeVat": "9.19", "isDelete": "1"},
    }}

    adjustment = carrier_service._raise_price_to_reported_floor(payload, error)

    assert adjustment == {"oldPrice": "9.19", "costFloor": "9.66",
                          "newPrice": "9.67", "skuKeys": ["active"]}
    assert payload["skuMap"]["active"] == {
        "price": "9.67", "priceIncludeVat": "9.67",
        "originPrice": "2.40", "isDelete": "0"}
    assert payload["skuMap"]["other"]["price"] == "12.00"
    assert payload["skuMap"]["deleted"]["price"] == "9.19"


def test_reported_cost_floor_equal_to_price_still_adds_one_cent():
    message = ("产品设置的本地展示价9.66MYR，无法覆盖产品基础成本"
               "(跨境物流运费+关税+增值税)：9.66MYR")
    payload = {"skuMap": {"sku-a": {"price": "9.66"}}}

    adjustment = carrier_service._raise_price_to_reported_floor(
        payload, MiaoshouError(message, payload={"message": message}))

    assert adjustment["newPrice"] == "9.67"
    assert payload["skuMap"]["sku-a"]["price"] == "9.67"


def test_reported_cost_floor_preserves_price_include_vat_ratio():
    message = ("产品设置的本地展示价35.90MYR，无法覆盖产品基础成本"
               "(跨境物流运费+关税+增值税)：36.00MYR")
    payload = {"skuMap": {"sku-a": {
        "price": "35.90", "priceIncludeVat": "39.49",
    }}}

    carrier_service._raise_price_to_reported_floor(
        payload, MiaoshouError(message, payload={"message": message}))

    assert payload["skuMap"]["sku-a"]["price"] == "36.01"
    assert payload["skuMap"]["sku-a"]["priceIncludeVat"] == "39.61"


def test_reported_cost_floor_can_raise_price_above_ten_percent_when_required():
    message = ("产品设置的本地展示价6.12MYR，无法覆盖产品基础成本"
               "(跨境物流运费+关税+增值税)：12.43MYR")
    payload = {"skuMap": {"sku-a": {
        "price": "6.12", "priceIncludeVat": "6.12", "originPrice": "10.00",
    }}}

    adjustment = carrier_service._raise_price_to_reported_floor(
        payload, MiaoshouError(message, payload={"message": message}))

    assert adjustment == {"oldPrice": "6.12", "costFloor": "12.43",
                          "newPrice": "12.44", "skuKeys": ["sku-a"]}
    assert payload["skuMap"]["sku-a"]["price"] == "12.44"


def test_fill_retries_increasing_reported_cost_floors_with_small_increments():
    class Client:
        def __init__(self):
            self.saved_bodies = []

        def shop_detail(self, *_args):
            return {"data": {"ossMd5": "version"}}

        def save_shop(self, _detail_id, _shop_id, _md5, body):
            self.saved_bodies.append(body["skuMap"]["sku-a"]["price"])
            floors = {1: "9.66", 2: "9.70"}
            floor = floors.get(len(self.saved_bodies))
            if floor:
                price = body["skuMap"]["sku-a"]["price"]
                message = (f"产品设置的本地展示价{price}MYR，无法覆盖产品基础成本"
                           f"(跨境物流运费+关税+增值税)：{floor}MYR")
                raise MiaoshouError(message, payload={"message": message})

    client = Client()
    result = carrier_service.fill_carriers(
        client, [1], [{"title": "a", "skuMap": {
            "sku-a": {"price": "9.19", "priceIncludeVat": "9.19",
                      "originPrice": "2.40"}}}],
        shop_id=9, warehouse_id="w1", verify_readback=False)

    assert result["saved"] == [1]
    assert len(client.saved_bodies) == 3
    assert client.saved_bodies == ["9.19", "9.67", "9.71"]
    assert result["priceAdjustments"] == [
        {"sourceIndex": 0, "oldPrice": "9.19", "costFloor": "9.66",
         "newPrice": "9.67", "skuKeys": ["sku-a"]},
        {"sourceIndex": 0, "oldPrice": "9.67", "costFloor": "9.70",
         "newPrice": "9.71", "skuKeys": ["sku-a"]},
    ]


def test_fill_stops_after_bounded_cost_floor_retries():
    class Client:
        def __init__(self):
            self.saved_bodies = []

        def shop_detail(self, *_args):
            return {"data": {"ossMd5": "version"}}

        def save_shop(self, _detail_id, _shop_id, _md5, body):
            self.saved_bodies.append(body["skuMap"]["sku-a"]["price"])
            price = body["skuMap"]["sku-a"]["price"]
            floor = float(price) + 1
            message = (f"产品设置的本地展示价{price}MYR，无法覆盖产品基础成本"
                       f"(跨境物流运费+关税+增值税)：{floor:.2f}MYR")
            raise MiaoshouError(message, payload={"message": message})

    client = Client()
    result = carrier_service.fill_carriers(
        client, [1], [{"title": "a", "skuMap": {
            "sku-a": {"price": "9.19", "priceIncludeVat": "9.19"}}}],
        shop_id=9, warehouse_id="w1", verify_readback=False)

    assert result["saved"] == []
    assert len(client.saved_bodies) == carrier_service.MAX_COST_FLOOR_RETRIES + 1
    assert result["failureDetails"][0]["certain"] is True


def test_fill_reports_item_level_known_and_unknown_failures():
    class Client:
        def shop_detail(self, detail_id, _shop_id):
            if detail_id == 2:
                raise MiaoshouError("request timeout", kind="timeout")
            return {"data": {"ossMd5": "version"}}

        def save_shop(self, detail_id, *_args):
            if detail_id == 1:
                raise MiaoshouError("detail image count exceeds 30", kind="error")

    result = carrier_service.fill_carriers(
        Client(), [1, 2], [{"title": "a"}, {"title": "b"}],
        shop_id=9, warehouse_id="w1", verify_readback=False)

    assert result["failureDetails"] == [
        {"detailId": "1", "certain": True,
         "reason": "detail image count exceeds 30"},
        {"detailId": "2", "certain": False, "reason": "request timeout"},
    ]


def test_fill_marks_http_server_errors_as_uncertain():
    class Client:
        def shop_detail(self, *_args):
            return {"data": {"ossMd5": "version"}}

        def save_shop(self, *_args):
            raise MiaoshouError("HTTP 502", status=502)

    result = carrier_service.fill_carriers(
        Client(), [1], [{"title": "a"}], shop_id=9,
        warehouse_id="w1", verify_readback=False)

    assert result["failureDetails"] == [
        {"detailId": "1", "certain": False, "reason": "HTTP 502"}]


def test_miaoshou_http_5xx_is_classified_as_unknown():
    class Breaker:
        def allow(self):
            return True

        def record_success(self):
            pass

    class Response:
        status_code = 502
        content = b'{"message":"gateway failure"}'

    class Transport:
        def post(self, *_args, **_kwargs):
            return Response()

    client = object.__new__(MiaoshouClient)
    client._breaker = Breaker()
    client._pacer = None
    client._client = Transport()
    client.app_key = "test-key"
    client.app_secret = "test-secret"
    client.base = "https://miaoshou.example"

    with pytest.raises(MiaoshouError) as caught:
        client._post_once("/write", {})

    assert caught.value.status == 502
    assert caught.value.kind == "unknown"


def test_miaoshou_protocol_failure_after_request_is_unknown():
    class Breaker:
        def allow(self):
            return True

        def record_transport_failure(self):
            pass

    class Transport:
        def post(self, *_args, **_kwargs):
            raise httpx.RemoteProtocolError("connection closed after request")

    client = object.__new__(MiaoshouClient)
    client._breaker = Breaker()
    client._pacer = None
    client._client = Transport()
    client.app_key = "test-key"
    client.app_secret = "test-secret"
    client.base = "https://miaoshou.example"

    with pytest.raises(MiaoshouError) as caught:
        client._post_once("/write", {})

    assert caught.value.kind == "unknown"


def test_carrier_creation_overlaps_with_covering(monkeypatch):
    events = []
    first_cover_started = threading.Event()

    def fake_collect(_db, _client, *, count, on_carrier, **_kwargs):
        assert count == 2
        on_carrier(1001)
        assert first_cover_started.wait(timeout=1)
        events.append("created-2")
        on_carrier(1002)
        return [1001, 1002]

    def fake_fill(_client, carriers, payloads, *, shop_id, verify_readback,
                  warehouse_id, **_kwargs):
        assert shop_id == 9
        assert verify_readback is False
        assert warehouse_id == "w1"
        events.append(f"cover-start-{carriers[0]}")
        first_cover_started.set()
        time.sleep(0.03)
        events.append(f"cover-done-{carriers[0]}")
        return {"saved": carriers, "savedSources": [0], "failures": []}

    monkeypatch.setattr(carrier_service, "target_warehouse_id",
                        lambda *_args: "w1")
    monkeypatch.setattr(carrier_service, "collect_carriers", fake_collect)
    monkeypatch.setattr(carrier_service, "fill_carriers", fake_fill)
    result = carrier_service.collect_and_fill_carriers(
        None, object(), payloads=[{"title": "a"}, {"title": "b"}], shop_id=9)

    assert events.index("created-2") < events.index("cover-done-1001")
    assert result["saved"] == [1001, 1002]
    assert result["savedSources"] == [0, 1]


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


def test_delayed_copy_receipt_recovers_draft_without_common_box_id(monkeypatch):
    class DelayedReadback(ExistingCommonBox):
        def __init__(self):
            super().__init__()
            self.box_ids = [900]
            self.list_calls = 0

        def claim_common_to_platform(self, items):
            self.calls.append(items)
            self.box_ids.append(1001)
            return {"data": {}}

        def collect_box_list(self, **kwargs):
            self.list_calls += 1
            rows = [{"collectBoxDetailId": 900,
                     "commonCollectBoxDetailId": 42}]
            if self.list_calls >= 4:
                rows.append({"collectBoxDetailId": 1001})
            return {"data": {"detailList": rows}}

    client = DelayedReadback()
    sleeps = []
    monkeypatch.setattr(carrier_service.time, "sleep", sleeps.append)

    ids = carrier_service.collect_carriers(None, client, count=1)

    assert ids == [1001]
    assert client.list_calls == 4  # one pre-copy snapshot and three readbacks
    assert client.calls == [[(42, "tiktok", 1)]]
    assert sleeps == [1, 1]


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


def test_missing_copy_ids_stops_without_using_existing_products(monkeypatch):
    client = ExistingCommonBox()
    client.claim_common_to_platform = lambda *_: {"data": {}}
    monkeypatch.setattr(carrier_service.time, "sleep", lambda *_: None)
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
