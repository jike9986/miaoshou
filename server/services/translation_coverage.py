"""翻译覆盖检查（老版 product_workflow.translation_coverage 的移植）。

为什么需要它：1688 的标题是中文，翻译接口返回的字段是「部分覆盖」的 ——
某些嵌套的 SKU 属性值、副标题别名可能没被翻译。只看容器级结果会把
「漏翻某个叶子字段」误判成翻译成功，最后上架一个中马混杂的商品。

这里逐叶子比对：源字段含中文、译文仍含中文（或译文为空）→ missing。
missing 非空时调用方应当阻止自动保存。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_URL = re.compile(r"^https?://", re.IGNORECASE)

# 这些字段承载身份、价格、数量、ID、图片位置，必须原样保留，不参与翻译检查。
PROTECTED_FIELDS = {
    "detailId", "shopId", "site", "editModel", "cid", "imgUrls", "skuMap",
    "weight", "packageLength", "packageWidth", "packageHeight", "sizeChart",
    "sizeChartType", "brandId", "manufacturerIds", "responsiblePersonIds",
    "isCodOpen", "productCertifications", "mainImgPlatformVideoId", "mainImgVideoUrl",
}

# 列表项按这些字段配对「源 -> 译文」，而不是纯按下标。
_STRUCTURAL_IDS = ("attrId", "attrValueId", "attributeId", "id")

TEXT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "subtitle": ("subTitle", "subtitle", "shortTitle", "productName"),
}


def _contains_cjk(value: Any) -> bool:
    return isinstance(value, str) and bool(_CJK.search(value))


def _field_status(source: Any, translated: Any, *, protected: bool = False) -> str:
    if source in (None, "", [], {}):
        return "not_present"
    if protected:
        return "preserved"
    if isinstance(source, str) and _URL.match(source):
        return "not_required"
    if not _contains_cjk(source):
        return "not_required"
    if translated in (None, "") or _contains_cjk(translated):
        return "missing"
    return "translated"


def _aggregate_status(statuses: Iterable[str]) -> str:
    values = list(statuses)
    if not values or all(value == "not_present" for value in values):
        return "not_present"
    if "missing" in values:
        return "missing"
    if "translated" in values:
        return "translated"
    if "preserved" in values:
        return "preserved"
    return "not_required"


def _coverage_tree(source: Any, translated: Any, path: str, *,
                   protected: bool = False) -> tuple[dict[str, str], list[str]]:
    """返回某个值及其所有嵌套文本路径的状态。

    翻译接口对 SKU 属性和商品属性返回嵌套数组。只检查容器会把「漏翻的值」
    标成已翻译，所以每个源叶子都单独判定。
    """
    if protected:
        return ({path: _field_status(source, translated, protected=True)}, [])
    if isinstance(source, str) or source is None or isinstance(source, (int, float, bool)):
        status = _field_status(source, translated)
        return ({path: status}, [path] if status == "missing" else [])

    if isinstance(source, Mapping):
        if not source:
            return ({path: "not_present"}, [])
        fields: dict[str, str] = {}
        missing: list[str] = []
        statuses: list[str] = []
        translated_mapping = translated if isinstance(translated, Mapping) else {}
        for key, value in source.items():
            child_path = f"{path}.{key}" if path else str(key)
            child_fields, child_missing = _coverage_tree(
                value, translated_mapping.get(key), child_path)
            fields.update(child_fields)
            missing.extend(child_missing)
            statuses.append(
                child_fields.get(child_path, _aggregate_status(child_fields.values())))
        fields[path] = _aggregate_status(statuses)
        if fields[path] == "missing":
            missing.append(path)
        return fields, missing

    if isinstance(source, list):
        if not source:
            return ({path: "not_present"}, [])
        fields = {}
        missing = []
        statuses = []
        translated_list = translated if isinstance(translated, list) else []
        translated_by_id = {
            (field, str(value[field])): value
            for value in translated_list if isinstance(value, Mapping)
            for field in _STRUCTURAL_IDS if value.get(field) not in (None, "")
        }
        for index, value in enumerate(source):
            child_path = f"{path}[{index}]"
            child_translated = None
            if isinstance(value, Mapping):
                for field in _STRUCTURAL_IDS:
                    if value.get(field) not in (None, ""):
                        child_translated = translated_by_id.get((field, str(value[field])))
                        if child_translated is not None:
                            break
            if child_translated is None and index < len(translated_list):
                positional = translated_list[index]
                if not isinstance(positional, Mapping) or not any(
                    positional.get(field) not in (None, "") for field in _STRUCTURAL_IDS
                ):
                    child_translated = positional
            child_fields, child_missing = _coverage_tree(value, child_translated, child_path)
            fields.update(child_fields)
            missing.extend(child_missing)
            statuses.append(
                child_fields.get(child_path, _aggregate_status(child_fields.values())))
        fields[path] = _aggregate_status(statuses)
        if fields[path] == "missing":
            missing.append(path)
        return fields, missing

    status = _field_status(source, translated)
    return ({path: status}, [path] if status == "missing" else [])


def translation_coverage(source: Mapping[str, Any], translated: Mapping[str, Any],
                         target_language: str = "ms") -> dict[str, Any]:
    """逐字段报告翻译覆盖情况，并列出仍有中文的路径。

    缺字段会显式出现在报告里；只要源字段本来含中文，就会阻止自动保存。
    """
    fields: dict[str, str] = {}
    missing: list[str] = []
    for key, value in source.items():
        if key in PROTECTED_FIELDS:
            continue
        translated_value = translated.get(key)
        if key == "brandName":
            fields[key] = _field_status(value, translated_value, protected=True)
            continue
        tree, tree_missing = _coverage_tree(value, translated_value, key)
        fields.update(tree)
        missing.extend(tree_missing)

    for alias in TEXT_FIELD_ALIASES["subtitle"]:
        if alias in source or alias in translated:
            fields.setdefault(
                alias, _field_status(source.get(alias), translated.get(alias)))

    missing.extend(name for name, status in fields.items() if status == "missing")
    missing = list(dict.fromkeys(missing))
    return {
        "targetLanguage": target_language,
        "requestedFields": fields,
        "missingFields": missing,
        "complete": not missing,
        "status": "complete" if not missing else "incomplete",
    }
