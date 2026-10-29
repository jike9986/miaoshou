"""主图 / SKU 图 / 详情图共用提交管线的回归测试。"""
from services import image_service, warehouse_service


def test_attach_image_translation_supports_sku_and_detail(monkeypatch):
    calls = []

    def fake_translate(db, payload, *, operator_id=None):
        calls.append((payload, operator_id))
        return {
            "batchId": "imgb-test",
            "jobs": [{"status": "submitted", "source": item["source"], "url": item["url"]}
                     for item in payload["items"]],
        }

    monkeypatch.setattr(image_service, "translate", fake_translate)
    product = {"id": "7", "detailId": "detail-7"}

    sku = warehouse_service.attach_image_translation(
        None, product, limit=2, source="sku",
        source_images=["https://img.test/sku-1.jpg", "https://img.test/sku-1.jpg",
                       "https://img.test/sku-2.jpg", "https://img.test/sku-3.jpg"],
        operator_id=3,
    )
    detail = warehouse_service.attach_image_translation(
        None, product, limit=1, source="detail",
        source_images=["https://img.test/detail-1.jpg", "https://img.test/detail-2.jpg"],
    )

    assert [item["source"] for item in calls[0][0]["items"]] == ["sku", "sku"]
    assert [item["url"] for item in calls[0][0]["items"]] == [
        "https://img.test/sku-1.jpg", "https://img.test/sku-2.jpg"]
    assert calls[0][1] == 3
    assert sku["requested"] == sku["submitted"] == 2
    assert sku["failed"] == 0
    assert calls[1][0]["items"] == [{
        "productId": "7", "detailId": "detail-7", "source": "detail",
        "url": "https://img.test/detail-1.jpg",
    }]
    assert detail["requested"] == detail["submitted"] == 1


def test_attach_image_translation_defaults_to_main_behavior():
    result = warehouse_service.attach_image_translation(
        None, {"id": "7"}, limit=5, source_images=[])
    assert result == {
        "requested": 0, "submitted": 0, "failed": 0, "jobs": [],
        "reason": "没有可翻译的主图",
    }
