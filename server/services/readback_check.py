"""保存读回校验（老版 product_workflow._readback_check 的等价实现）。

保存到妙手之后重新拉一次商品，逐项比对「提交的」和「真的存下来的」是不是一致。
没有这一步，保存接口返回 200 就当作成功——但平台可能静默丢掉字段：
标题被截断、详情图被过滤、价格被改写、翻译没生效。用户要等上架后才发现。

比对项与老版一致：
    title / subtitle / categoryCid / weight / packageDimensions / sizeChart /
    mainImages / detailImages / skuImages / prices / translations

prices 的判定有例外：开了定价时 `price` 本来就该变，但 `originPrice` 必须原样返回。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# 副标题在不同站点用不同字段名，与老版 TEXT_FIELD_ALIASES 一致
_SUBTITLE_FIELDS = ("subTitle", "subtitle", "sellingPoint")

# 价格字段：开定价后允许变的
_PRICE_FIELDS = ("price", "priceIncludeVat", "shopPrice", "salePrice")
# 无论开不开定价都不该变的
_ORIGIN_PRICE_FIELDS = ("originPrice", "originPriceIncludeVat", "purchasePrice")


def contains_cjk(value: Any) -> bool:
    """值里还有没有中文。翻译是否真的生效，看这个。"""
    if isinstance(value, str):
        return bool(_CJK.search(value))
    if isinstance(value, Mapping):
        return any(contains_cjk(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_cjk(item) for item in value)
    return False


def get_path(source: Any, path: str) -> Any:
    """按 `a.b[0].c` 取值；取不到返回 None（与老版 _get_path 一致）。"""
    current = source
    for raw in str(path or "").split("."):
        if not raw:
            continue
        name, _, rest = raw.partition("[")
        if name:
            if not isinstance(current, Mapping):
                return None
            current = current.get(name)
        while rest:
            index_text, _, rest = rest.partition("]")
            if not rest and "]" not in index_text:
                # 没有闭合的 ]，说明这个 path 写法有问题，当作取不到
                pass
            if not str(index_text).strip().isdigit():
                return None
            index = int(index_text)
            if not isinstance(current, (list, tuple)) or index >= len(current):
                return None
            current = current[index]
            rest = rest.lstrip("[")
    return current


def extract_detail_image_urls(notes: Any) -> list[str]:
    """从详情 HTML 里按顺序取出 <img src>（老版 extract_detail_image_urls）。"""
    if not isinstance(notes, str) or not notes:
        return []
    import html as _html

    urls: list[str] = []
    for match in re.finditer(r"<img\b[^>]*>", notes, re.IGNORECASE):
        tag = match.group(0)
        found = re.search(r"""(?:src)\s*=\s*["']([^"']+)["']""", tag, re.IGNORECASE)
        if found:
            urls.append(_html.unescape(found.group(1)).strip())
    return [url for url in urls if url]


def sku_image_refs(item: Any) -> list[dict[str, str]]:
    """SKU 图片引用：`{path, url}` 列表，按路径排序后比对。"""
    properties = item.get("skuPropertyList") if isinstance(item, Mapping) else item
    refs: list[dict[str, str]] = []
    if not isinstance(properties, list):
        return refs
    for prop_index, prop in enumerate(properties):
        if not isinstance(prop, Mapping):
            continue
        for value_index, value in enumerate(prop.get("attrValueList") or []):
            if not isinstance(value, Mapping):
                continue
            url = str(value.get("imageUrl") or value.get("image") or "").strip()
            if url:
                refs.append({
                    "path": f"skuPropertyList[{prop_index}].attrValueList[{value_index}]",
                    "url": url,
                })
    return refs


def non_image_data(item: Mapping[str, Any]) -> dict[str, Any]:
    """去掉所有图片字段后的商品数据（image_only 模式用来确认没动别的）。"""
    result = deepcopy(dict(item))
    result.pop("imgUrls", None)
    result.pop("notes", None)
    for prop in result.get("skuPropertyList") or []:
        if not isinstance(prop, Mapping):
            continue
        for value in prop.get("attrValueList") or []:
            if isinstance(value, Mapping):
                value.pop("imageUrl", None)
                value.pop("image", None)
    return result


def _attribute_map(attributes: Any) -> dict[str, set[str]] | None:
    """productAttributes -> {attributeId: {value,...}}；结构不认识就返回 None。"""
    if not isinstance(attributes, list):
        return None
    result: dict[str, set[str]] = {}
    for attribute in attributes:
        if not isinstance(attribute, Mapping):
            return None
        attribute_id = str(attribute.get("attributeId") or attribute.get("id") or "").strip()
        if not attribute_id:
            return None
        values = {
            str(value.get("attributeValue") or value.get("value") or "").strip()
            for value in (attribute.get("attributeValues") or [])
            if isinstance(value, Mapping)
        }
        values.discard("")
        result.setdefault(attribute_id, set()).update(values)
    return result


def attributes_match(expected: Any, observed: Any) -> bool:
    """比对 productAttributes，不依赖顺序/命名/去重。

    妙手把它们当无序集合：读回时省略 attributeName、可能重排、还会把重复值合并
    （提交五个 EVA，读回只有一个 EVA）。按位置比会把「其实存成功了」判成失败。
    所以按 attributeId 做集合比：改过或凭空多出来的值仍然抓得住。
    """
    expected_map = _attribute_map(expected)
    observed_map = _attribute_map(observed)
    if expected_map is None or observed_map is None:
        return expected == observed
    if set(expected_map) != set(observed_map):
        return False
    return all(expected_map[key] == observed_map[key] for key in expected_map)


def translation_readback_matches(payload: Mapping[str, Any], actual: Mapping[str, Any],
                                 coverage: Mapping[str, Any]) -> bool:
    """翻译覆盖里标了 translated 的字段，读回必须相同且不含中文。"""
    fields = coverage.get("requestedFields") or {}
    attributes_checked = False
    for path, status in fields.items():
        if status != "translated":
            continue
        if path == "productAttributes" or str(path).startswith("productAttributes["):
            # 索引路径在上游重排后就没意义了，整个集合比一次即可
            if attributes_checked:
                continue
            attributes_checked = True
            if not attributes_match(payload.get("productAttributes"),
                                    actual.get("productAttributes")):
                return False
            if contains_cjk(actual.get("productAttributes")):
                return False
            continue
        expected = get_path(payload, str(path))
        observed = get_path(actual, str(path))
        if expected != observed or contains_cjk(observed):
            return False
    return True


def _number(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _prices_unchanged(original: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    """不开定价时，价格必须原样回来。"""
    for field in _PRICE_FIELDS:
        if _number(original.get(field)) != _number(actual.get(field)):
            return False
    original_sku = original.get("skuMap") or {}
    actual_sku = actual.get("skuMap") or {}
    if not isinstance(original_sku, Mapping) or not isinstance(actual_sku, Mapping):
        return True
    for key, sku in original_sku.items():
        if not isinstance(sku, Mapping):
            continue
        observed = actual_sku.get(key) or {}
        for field in _PRICE_FIELDS:
            if _number(sku.get(field)) != _number(observed.get(field)):
                return False
    return True


def _prices_match(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    """读回销售价必须与本地应用模板后的目标价格一致。"""
    for field in _PRICE_FIELDS:
        if _number(expected.get(field)) != _number(actual.get(field)):
            return False

    expected_sku = expected.get("skuMap") or {}
    actual_sku = actual.get("skuMap") or {}
    if not isinstance(expected_sku, Mapping):
        return True
    if expected_sku and not isinstance(actual_sku, Mapping):
        return False
    if not isinstance(actual_sku, Mapping):
        return True
    for key, sku in expected_sku.items():
        if not isinstance(sku, Mapping):
            continue
        observed = actual_sku.get(key)
        if not isinstance(observed, Mapping):
            if any(sku.get(field) not in (None, "") for field in _PRICE_FIELDS):
                return False
            continue
        for field in _PRICE_FIELDS:
            if _number(sku.get(field)) != _number(observed.get(field)):
                return False
    return True


def pricing_preserves_origin_price(original: Mapping[str, Any],
                                   actual: Mapping[str, Any]) -> bool:
    """开了定价：`price` 可以变，`originPrice` 不能被改掉。

    采购价被模板顺手改掉是最隐蔽的一类事故——下次按模板重算时基数就错了。
    """
    for field in _ORIGIN_PRICE_FIELDS:
        if _number(original.get(field)) != _number(actual.get(field)):
            return False
    original_sku = original.get("skuMap") or {}
    actual_sku = actual.get("skuMap") or {}
    if not isinstance(original_sku, Mapping) or not isinstance(actual_sku, Mapping):
        return True
    for key, sku in original_sku.items():
        if not isinstance(sku, Mapping):
            continue
        observed = actual_sku.get(key) or {}
        for field in _ORIGIN_PRICE_FIELDS:
            if _number(sku.get(field)) != _number(observed.get(field)):
                return False
    return True


def _retained_sku_images_unchanged(original: Mapping[str, Any],
                                   actual: Mapping[str, Any]) -> bool:
    """保留范围内的 SKU 图片没被换掉。"""
    return sku_image_refs(original) == sku_image_refs(actual)


def readback_issues(payload: Mapping[str, Any], actual: Mapping[str, Any],
                    original: Mapping[str, Any], coverage: Mapping[str, Any], *,
                    pricing_enabled: bool = False,
                    image_only: bool = False,
                    translate_images: bool = False) -> list[str]:
    """逐项比对；返回不通过的字段名（空列表＝真的存进去了）。

    与老版 `_readback_check` 的 comparisons 字典一一对应，顺序也保持一致，
    这样两份实现对同一批数据给出的结论相同。
    """
    comparisons: dict[str, bool] = {}

    comparisons["title"] = actual.get("title") == payload.get("title")

    subtitle_fields = [field for field in _SUBTITLE_FIELDS if field in payload]
    comparisons["subtitle"] = all(
        actual.get(field) == payload.get(field) for field in subtitle_fields)

    comparisons["categoryCid"] = actual.get("cid") == payload.get("cid")

    payload_sku = payload.get("skuMap") or {}
    actual_sku = actual.get("skuMap") or {}
    weight_ok = actual.get("weight") == payload.get("weight")
    if isinstance(payload_sku, Mapping) and isinstance(actual_sku, Mapping):
        for key, sku in payload_sku.items():
            if not isinstance(sku, Mapping):
                continue
            if (actual_sku.get(key) or {}).get("weight") != sku.get("weight"):
                weight_ok = False
                break
    comparisons["weight"] = weight_ok

    comparisons["packageDimensions"] = all(
        actual.get(field) == payload.get(field)
        for field in ("packageLength", "packageWidth", "packageHeight"))

    comparisons["sizeChart"] = actual.get("sizeChart") == payload.get("sizeChart")

    comparisons["mainImages"] = actual.get("imgUrls") == payload.get("imgUrls")

    comparisons["detailImages"] = (
        extract_detail_image_urls(actual.get("notes"))
        == extract_detail_image_urls(payload.get("notes")))

    comparisons["skuImages"] = _retained_sku_images_unchanged(payload, actual)

    if pricing_enabled:
        # 开定价时销售价必须与处理结果一致，采购价必须原样返回。
        comparisons["prices"] = _prices_match(payload, actual)
        comparisons["originPriceUnchanged"] = pricing_preserves_origin_price(original, actual)
    else:
        comparisons["prices"] = _prices_unchanged(original, actual)

    comparisons["translations"] = translation_readback_matches(payload, actual, coverage)

    if image_only:
        comparisons["nonImageFields"] = (
            non_image_data(original) == non_image_data(actual))

    if translate_images:
        from services import pre_save_check

        comparisons["detailImagesOnly"] = (
            actual.get("notes", "") == pre_save_check.detail_images_only(
                extract_detail_image_urls(payload.get("notes"))))

    return [name for name, passed in comparisons.items() if not passed]


def details(payload: Mapping[str, Any], actual: Mapping[str, Any],
            original: Mapping[str, Any], coverage: Mapping[str, Any], *,
            pricing_enabled: bool = False, image_only: bool = False,
            translate_images: bool = False) -> list[dict[str, Any]]:
    """给界面用的逐项结果（老版 renderDiagnostics 吃的是这个形状）。"""
    failed = set(readback_issues(
        payload, actual, original, coverage,
        pricing_enabled=pricing_enabled, image_only=image_only,
        translate_images=translate_images))
    checked: Iterable[str] = (
        "title", "subtitle", "categoryCid", "weight", "packageDimensions", "sizeChart",
        "mainImages", "detailImages", "skuImages", "prices", "translations",
    )
    rows = [{"field": name, "status": "failed" if name in failed else "passed"}
            for name in checked]
    if pricing_enabled:
        rows.append({"field": "originPriceUnchanged",
                     "status": "failed" if "originPriceUnchanged" in failed else "passed"})
    if image_only:
        rows.append({"field": "nonImageFields",
                     "status": "failed" if "nonImageFields" in failed else "passed"})
    if translate_images:
        rows.append({"field": "detailImagesOnly",
                     "status": "failed" if "detailImagesOnly" in failed else "passed"})
    return rows
