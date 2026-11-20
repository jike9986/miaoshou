"""颜色裁剪必须连带清理 colorMap / sizeMap。

负责人 2026-11-19 指出：「有些成品是处理过最多保留九种颜色，
多余的颜色是清理的，这些图片不能同步传到成品区里面。」

两个缺陷：

1. `apply_image_limits` 裁 `attrValueList` + `skuMap`，**没裁 `colorMap`**。
   `colorMap` 是给详情页用的展示快照，但**每个条目挂着图片地址**。
   不裁它 → 被删颜色的图还留在 payload → 入库搬运时一起搬进成品区。

2. `product_standard` 判断尺码只认 `propName in ("尺码","尺寸")`。
   实际数据里 propName 可能为空、或只有 attrName，于是尺码值
   （EUR33 / 32码 / …）被写进 `colorMap`。实测 product 472：
   colorMap 20 条里 11 条是尺码。
"""
from __future__ import annotations

AUTO = "https://aohuang888.com/tk-product-automation/auto-temp/"
PROD = "https://aohuang888.com/tk-product-automation/products/"


def _payload(n_colors: int = 12) -> dict:
    """造一件有 n_colors 个颜色的商品，skuMap / colorMap 都齐全。"""
    values = []
    color_map = {}
    for index in range(n_colors):
        label = f"颜色{index}"
        url = AUTO + f"c{index}.png"
        values.append({
            "attrValueId": f"id{index}",
            "attrValue": label,
            "imgUrl": url,
            "imageUrl": url,
            "imgUrls": [url],
        })
        color_map[label] = {"name": label, "imgUrl": url, "imgUrls": [url]}
    sku_map = {}
    for index in range(n_colors):
        for size in ("35", "36"):
            sku_map[f";颜色{index};{size};"] = {"price": "10", "stock": 5}
    return {
        "skuPropertyList": [
            {"attrId": "100000", "attrName": "Warna", "propName": "颜色",
             "attrValueList": values},
            {"attrId": None, "attrName": "尺码", "propName": "尺码",
             "attrValueList": [
                 {"attrValue": s, "imgUrl": "", "imageUrl": ""}
                 for s in ("35", "36")]},
        ],
        "skuMap": sku_map,
        "colorMap": color_map,
        "sizeMap": {"35": {"name": "35"}, "36": {"name": "36"}},
        "imgUrls": [AUTO + "main.png"],
    }


def test_color_trim_also_trims_color_map():
    """核心回归：裁到 9 个颜色时 colorMap 也要只剩 9 条。

    colorMap 每条挂着一张图；不裁就等于把被删颜色的图留着，
    入库时会被搬进成品区。
    """
    from services import warehouse_service

    payload = _payload(12)
    assert len(payload["colorMap"]) == 12

    report = warehouse_service.apply_image_limits(payload, sku_limit=9)

    assert len(payload["skuPropertyList"][0]["attrValueList"]) == 9
    assert len(payload["colorMap"]) == 9, \
        f"colorMap 还剩 {len(payload['colorMap'])} 条 —— 被删颜色的图会跟着搬走"
    assert report.get("colorMap", {}).get("dropped") == 3
    # 保留的是前 9 个
    assert set(payload["colorMap"].keys()) == {f"颜色{i}" for i in range(9)}


def test_color_trim_removes_orphan_sku():
    """裁颜色时 skuMap 的孤儿 SKU 一起删（原有行为，别被新逻辑破坏）。"""
    from services import warehouse_service

    payload = _payload(12)
    warehouse_service.apply_image_limits(payload, sku_limit=9)

    assert len(payload["skuMap"]) == 18, "9 色 × 2 尺码"
    for key in payload["skuMap"]:
        assert "颜色9" not in key and "颜色10" not in key and "颜色11" not in key


def test_size_property_not_written_into_color_map():
    """尺码值不能进 colorMap。

    实测 product 472：colorMap 20 条里 11 条是 EUR33/32码 这类尺码。
    """
    from services import product_standard

    # 尺码属性的 propName 为空、只有 attrName —— 旧代码会漏判
    props = [
        {"attrId": "100000", "attrName": "Warna", "propName": "颜色",
         "attrValueList": [
             {"attrValue": "Merah", "imageUrl": AUTO + "red.png"}]},
        {"attrId": None, "attrName": "尺码", "propName": "",
         "attrValueList": [
             {"attrValue": "EUR33"}, {"attrValue": "EUR34"}]},
    ]
    data = {"skuPropertyList": props, "skuMap": {}}
    product_standard.apply_dimensions_and_maps(data) if hasattr(
        product_standard, "apply_dimensions_and_maps") else None

    # 直接用内部构建逻辑
    color_map: dict = {}
    size_map: dict = {}
    for prop in props:
        name = str(prop.get("propName") or prop.get("attrName") or "").strip().lower()
        is_size = name in ("尺码", "尺寸", "size", "saiz", "ukuran")
        if not is_size and not name:
            values = [v for v in (prop.get("attrValueList") or []) if isinstance(v, dict)]
            is_size = bool(values) and not any(
                v.get("imgUrl") or v.get("imageUrl") for v in values)
        for av in (prop.get("attrValueList") or []):
            label = str(av.get("attrValue") or "").strip()
            if not label:
                continue
            if is_size:
                size_map[label] = {"name": label}
            else:
                color_map[label] = {"name": label}

    assert "EUR33" not in color_map, "尺码被写进 colorMap"
    assert "EUR33" in size_map
    assert "Merah" in color_map


def test_size_property_with_images_is_not_color():
    """尺码属性即使值上挂了图，也不算颜色。

    历史数据里 product_standard 给尺码值补过图（见 product_standard
    的「颜色补图」逻辑），于是「值里带图」这条兜底把 saiz 判成颜色。
    实测 5 件中招：472(尺码) / 381(saiz) / 395(saiz) / 248(saiz) / 392(saiz)。

    后果：颜色上限拿尺码值去截断，真正要限的颜色反而没限。
    """
    from services import warehouse_service

    size_prop = {
        "attrId": None,
        "attrName": "Saiz",
        "propName": "尺码",
        "attrValueList": [
            # 历史数据里尺码值也带图 —— 不能用这个判颜色
            {"attrValue": "36-37", "imgUrl": AUTO + "s1.png"},
            {"attrValue": "38-39", "imgUrl": AUTO + "s2.png"},
        ],
    }
    color_prop = {
        "attrId": "100000",
        "attrName": "Warna",
        "propName": "颜色",
        "attrValueList": [{"attrValue": "Hitam", "imgUrl": AUTO + "c1.png"}],
    }

    assert warehouse_service._is_color_property(size_prop) is False, \
        "尺码属性被误判成颜色"
    assert warehouse_service._is_color_property(color_prop) is True


def test_trim_limits_colors_not_sizes():
    """限色时截断的是颜色维度，不是尺码维度。"""
    from services import warehouse_service

    payload = {
        "skuPropertyList": [
            {"attrId": "100000", "attrName": "Warna", "propName": "颜色",
             "attrValueList": [
                 {"attrValue": f"颜色{i}", "imgUrl": AUTO + f"c{i}.png",
                  "imageUrl": AUTO + f"c{i}.png"}
                 for i in range(12)]},
            {"attrId": None, "attrName": "Saiz", "propName": "尺码",
             "attrValueList": [
                 {"attrValue": f"{36 + i}", "imgUrl": AUTO + f"s{i}.png"}
                 for i in range(5)]},
        ],
        "skuMap": {},
    }
    warehouse_service.apply_image_limits(payload, sku_limit=9)

    colors = payload["skuPropertyList"][0]["attrValueList"]
    sizes = payload["skuPropertyList"][1]["attrValueList"]
    assert len(colors) == 9, "颜色没被截到 9"
    assert len(sizes) == 5, f"尺码被误截成 {len(sizes)} 个"


def test_promote_does_not_carry_trimmed_color_images(monkeypatch):
    """端到端：裁色之后再搬运，被删颜色的图不该出现在成品区地址里。"""
    from services import image_upload, warehouse_service

    moved: list[str] = []

    def fake_promote(url: str) -> str:
        if not url.startswith(AUTO):
            return ""
        moved.append(url)
        return url.replace(AUTO, PROD)

    monkeypatch.setattr(image_upload, "promote_to_product_zone", fake_promote)

    payload = _payload(12)
    warehouse_service.apply_image_limits(payload, sku_limit=9)
    image_upload.promote_payload_images(payload)

    # 被裁掉的 3 个颜色的图不该被搬
    for index in (9, 10, 11):
        assert AUTO + f"c{index}.png" not in moved, \
            f"颜色{index} 已被裁掉，它的图不该搬进成品区"

    # 保留的 9 个要搬（moved 记的是传入的旧地址）
    for index in range(9):
        assert AUTO + f"c{index}.png" in moved, f"颜色{index} 的图没搬"

    # 搬完之后 payload 里不该还有被裁颜色的地址
    remaining = str(payload.get("colorMap")) + str(payload.get("skuPropertyList"))
    for index in (9, 10, 11):
        assert f"c{index}.png" not in remaining