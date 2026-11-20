"""图片入库搬运：SKU 图必须真的从临时区搬到成品区。

2026-11-19 查明的两个 bug（都在生产数据里造成实际影响）：

1. `promote_payload_images` 里遍历 `sku_image_sources()` 的写法是
   `item.get("imgUrl")` —— 但那个函数返回的是
   `{path, kind, url, propertyIndex, ...}`，根本没有 `imgUrl` 这个 key。
   `item.get(field)` 恒为 None，**循环体从来没执行过**，SKU 图一张都没搬过。

2. `sku_image_sources` / `rewrite_sku_image_urls` 只认 `imgUrl` 和
   `supplementarySkuImageUrls`。妙手新数据用 `imageUrl` 和 `imgUrls[]`，
   这两个字段不搬也不替换。

实测生产：180 件成品仓商品里 **95 件**的图片 URL 停在 `auto-temp`
（367 处 imgUrl 搬走了，但 339 处 imageUrl + 339 处 imgUrls[] 没搬）。
`auto-temp` 是 14 天自动清理的临时目录 —— 到点商品图全变挂图。
"""
from __future__ import annotations

AUTO = "https://aohuang888.com/tk-product-automation/auto-temp/"
PROD = "https://aohuang888.com/tk-product-automation/products/"


def _payload() -> dict:
    return {
        "imgUrls": [AUTO + "main1.png", AUTO + "main2.png"],
        # 妙手新格式：imageUrl + imgUrls[]（一色多图）
        "skuPropertyList": [
            {
                "attrName": "颜色",
                "attrValueList": [
                    {
                        "attrValueId": "1",
                        "attrValue": "红色",
                        "imgUrl": AUTO + "sku-red.png",
                        "imageUrl": AUTO + "sku-red.png",
                        "imgUrls": [AUTO + "sku-red-1.png", AUTO + "sku-red-2.png"],
                        "supplementarySkuImageUrls": [AUTO + "sku-red-sup.png"],
                    },
                    {
                        "attrValueId": "2",
                        "attrValue": "蓝色",
                        "imgUrl": AUTO + "sku-blue.png",
                        "imageUrl": AUTO + "sku-blue.png",
                    },
                ],
            }
        ],
        "colorMap": {"红色": AUTO + "color-red.png"},
        "sizeChart": AUTO + "size.png",
        "notes": f'<img src="{AUTO}detail1.png"><img src="{AUTO}detail2.png">',
    }


def test_sku_image_sources_enumerates_all_four_fields():
    """三个图片字段 + 补充图都要枚举到。

    只认 imgUrl 的话，imageUrl / imgUrls[] 永远搬不走。
    """
    from services import warehouse_service

    payload = _payload()
    sources = warehouse_service.sku_image_sources(payload)
    urls = {item["url"] for item in sources}

    assert AUTO + "sku-red.png" in urls, "imgUrl 没枚举到"
    assert AUTO + "sku-red-1.png" in urls, "imgUrls[] 没枚举到"
    assert AUTO + "sku-red-2.png" in urls, "imgUrls[] 第二个没枚举到"
    assert AUTO + "sku-red-sup.png" in urls, "supplementarySkuImageUrls 没枚举到"

    kinds = {item["kind"] for item in sources}
    assert "imageUrl" in kinds, "imageUrl 字段没被当作独立来源"
    assert "imgUrls" in kinds, "imgUrls 字段没被当作独立来源"


def test_rewrite_replaces_all_four_fields():
    """替换要把四个字段全改掉，不能只改 imgUrl。"""
    from services import warehouse_service

    payload = _payload()
    replacements = {
        AUTO + "sku-red.png": PROD + "sku-red.png",
        AUTO + "sku-red-1.png": PROD + "sku-red-1.png",
        AUTO + "sku-red-2.png": PROD + "sku-red-2.png",
        AUTO + "sku-red-sup.png": PROD + "sku-red-sup.png",
    }
    result = warehouse_service.rewrite_sku_image_urls(
        payload["skuPropertyList"], replacements)

    value = result[0]["attrValueList"][0]
    assert value["imgUrl"] == PROD + "sku-red.png"
    assert value["imageUrl"] == PROD + "sku-red.png", "imageUrl 没被替换"
    assert value["imgUrls"] == [
        PROD + "sku-red-1.png", PROD + "sku-red-2.png"], "imgUrls[] 没被替换"
    assert value["supplementarySkuImageUrls"] == [PROD + "sku-red-sup.png"]

    # 平台主键和译文必须一字不动
    assert value["attrValueId"] == "1"
    assert value["attrValue"] == "红色"


def test_rewrite_does_not_mutate_input():
    """deepcopy 保证调用方手里的「翻译前」快照不被污染。"""
    from services import warehouse_service

    payload = _payload()
    original = payload["skuPropertyList"][0]["attrValueList"][0]["imgUrl"]
    warehouse_service.rewrite_sku_image_urls(
        payload["skuPropertyList"], {original: PROD + "x.png"})
    assert payload["skuPropertyList"][0]["attrValueList"][0]["imgUrl"] == original


def test_promote_moves_sku_images_not_just_main(monkeypatch):
    """核心回归：入库搬运必须真的动到 SKU 图。

    旧代码 `for item in sku_image_sources(payload): item.get("imgUrl")`
    循环体从不执行 —— 这个测试就是钉住那个 bug。
    """
    from services import image_upload

    moved: list[str] = []

    def fake_promote(url: str) -> str:
        if not url.startswith(AUTO):
            return ""
        moved.append(url)
        return url.replace(AUTO, PROD)

    monkeypatch.setattr(image_upload, "promote_to_product_zone", fake_promote)

    payload = _payload()
    report = image_upload.promote_payload_images(payload)

    # SKU 图必须被搬：red 的 imgUrl/imageUrl 是同一张，去重后算一次
    sku_moved = [u for u in moved if "sku-" in u]
    assert sku_moved, "SKU 图一张都没搬 —— item.get('imgUrl') 那个 bug 回来了"

    value = payload["skuPropertyList"][0]["attrValueList"][0]
    assert value["imgUrl"].startswith(PROD), "SKU imgUrl 还是临时区地址"
    assert value["imageUrl"].startswith(PROD), "SKU imageUrl 还是临时区地址"
    assert all(u.startswith(PROD) for u in value["imgUrls"]), "imgUrls[] 还是临时区"
    assert report["moved"] > 0


def test_promote_leaves_upstream_urls_alone(monkeypatch):
    """上游（1688 等）地址不能被改动。"""
    from services import image_upload

    monkeypatch.setattr(image_upload, "promote_to_product_zone",
                        lambda url: "" if not url.startswith(AUTO) else url.replace(AUTO, PROD))

    upstream = "https://cbu01.alicdn.com/img/abc.jpg"
    payload = {
        "imgUrls": [upstream],
        "skuPropertyList": [{"attrValueList": [
            {"imgUrl": upstream, "imageUrl": upstream,
             "attrValueId": "9", "attrValue": "x"}]}],
        "notes": f'<img src="{upstream}">',
    }
    image_upload.promote_payload_images(payload)

    assert payload["imgUrls"] == [upstream]
    assert payload["skuPropertyList"][0]["attrValueList"][0]["imgUrl"] == upstream
    assert upstream in payload["notes"]


def test_promote_covers_color_map(monkeypatch):
    """colorMap 也要搬 —— 实测 #511 搬走 24 处后剩的 8 处全在这里。

    colorMap 有两种形态：
      {"红色": "https://…"}                       —— 直接是 URL
      {"红色": {"imgUrl": "…", "imgUrls": [...]}}  —— 结构体
    """
    from services import image_upload

    monkeypatch.setattr(image_upload, "promote_to_product_zone",
                        lambda url: "" if not url.startswith(AUTO) else url.replace(AUTO, PROD))

    payload = {
        "colorMap": {
            "红色": AUTO + "c-red.png",
            "蓝色": {"imgUrl": AUTO + "c-blue.png",
                     "imageUrl": AUTO + "c-blue.png",
                     "imgUrls": [AUTO + "c-blue-1.png"]},
        },
        "skuPropertyList": [],
    }
    image_upload.promote_payload_images(payload)

    assert payload["colorMap"]["红色"].startswith(PROD), "字符串形态的颜色图没搬"
    blue = payload["colorMap"]["蓝色"]
    assert blue["imgUrl"].startswith(PROD), "colorMap.imgUrl 没搬"
    assert blue["imageUrl"].startswith(PROD), "colorMap.imageUrl 没搬"
    assert all(u.startswith(PROD) for u in blue["imgUrls"]), "colorMap.imgUrls[] 没搬"


def test_promote_skips_zone_cleanup_when_source_gone(monkeypatch):
    """源文件已被搬走（成品区有同名）时，只改地址，不报错。

    实测生产：95 件商品的图都是这个状态 —— 入库时文件搬成功了
    （promote_to_product_zone 里会 unlink 临时区原件），但 payload 里的
    URL 因为上面的 bug 没跟着改。文件在 products、地址指着 auto-temp。
    """
    from pathlib import Path

    from services import image_upload

    tmp = Path(__file__).parent / "_zone_tmp"
    auto_dir = tmp / "auto-temp"
    prod_dir = tmp / "products"
    auto_dir.mkdir(parents=True, exist_ok=True)
    prod_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 临时区没有、成品区有同名文件
        name = "gone-from-auto.png"
        (prod_dir / name).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        monkeypatch.setenv("AOHUANG_IMAGE_LOCAL_DIR", str(auto_dir))
        monkeypatch.setenv("AOHUANG_IMAGE_PRODUCT_DIR", str(prod_dir))
        monkeypatch.setenv(
            "AOHUANG_IMAGE_LOCAL_BASE",
            "https://aohuang888.com/tk-product-automation/auto-temp/")
        monkeypatch.setenv(
            "AOHUANG_IMAGE_PRODUCT_BASE",
            "https://aohuang888.com/tk-product-automation/products/")

        result = image_upload.promote_to_product_zone(AUTO + name)
        assert result.endswith("/products/" + name), \
            "源文件不在但成品区有同名时，应该直接把地址改到成品区"
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_promote_is_idempotent(monkeypatch):
    """已经搬过的地址再跑一次不该重复动。"""
    from services import image_upload

    monkeypatch.setattr(image_upload, "promote_to_product_zone",
                        lambda url: "" if not url.startswith(AUTO) else url.replace(AUTO, PROD))

    payload = _payload()
    image_upload.promote_payload_images(payload)
    first = payload["skuPropertyList"][0]["attrValueList"][0]["imgUrl"]

    image_upload.promote_payload_images(payload)
    second = payload["skuPropertyList"][0]["attrValueList"][0]["imgUrl"]

    assert first == second
    assert second.startswith(PROD)