"""保存前校验（老版 product_workflow._pre_save_issues 的移植）。

存在的理由：入仓前必须挡住「看起来能存、实际会被平台拒掉或上架出错」的商品。
老版靠这 10 条规则把问题拦在写库之前，新版直接入库，等于把所有检查推给平台。

每条规则对应一次真实的踩坑：
  · 译文还有中文 → 上架后标题中马混杂
  · cid 缺失 → 平台无类目，保存被拒
  · 包裹尺寸与设置不符 → 运费算错
  · 尺码图缺失 → 平台要求必填
  · 主图为空 / 超限 → 保存被拒
  · 详情图超平台硬上限（30）→ 保存被拒，与本地设置无关
  · 详情含文字 → 平台详情区只接受图片
  · 关掉图片处理却改了图片字段 → 说明有别的流程动了不该动的字段
  · 价格被改动但没开定价 → 有东西意外改了价格
  · 开了定价但动了 originPrice → 采购价是记录，不是旋钮
  · 类目要求包裹尺寸却没填全 → 上架被平台拒（规则 11，需传 requirements）
  · 类目必填属性没填 → 上架被平台拒（规则 12，需传 requirements）

规则 11/12 是后加的：前 10 条只看商品自己，不知道「这个类目额外要求什么」。
老版把这两条放在 `shoe_workflow.validate_category_requirements`（:160-180），
依赖一次 category_metadata 上游调用。新版把「取要求」留在调用方
（warehouse_service / 路由），这里只按传进来的 requirements 判定 ——
本模块仍是纯函数、零上游依赖，单测和预览都不会被拖慢。

追加的规则 13/14（标题 review 词 / 主图 OCR 待复核）对应老版两道硬阻断：
`product_workflow.prepare_product` 的「标题清理后仍需要人工复核」和
`shoe_workflow.review_main_images` 的 "Image classification needs review"。
新版这两项只写进 report、不阻断入仓，含复核词的标题和带中文的主图能直接进库。

为什么用**可选参数**而不是新增一个函数：`pre_save_issues` 有多个调用方
（入仓、批量预览、只读检查），签名一变全部行为就跟着变。这里的做法是
「不传就是老行为」——`title_review_words` 默认 None 表示「本次不查标题复核词」，
`image_review_pending` 默认 None 表示「本次不查主图 OCR」。两条规则各自独立，
老调用方一行不改也拿不到任何新 issue。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any

_DETAIL_HARD_LIMIT = 30
_IMG_TAG = re.compile(r"<img\b[^>]*>", flags=re.IGNORECASE)
_IMG_SRC = re.compile(r"(\bsrc\s*=\s*)(['\"])(.*?)\2", flags=re.IGNORECASE | re.DOTALL)


class _DetailImageParser(HTMLParser):
    """按出现顺序取出详情 HTML 里的绝对图片 URL。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        for name, value in attrs:
            if name.lower() == "src" and value:
                url = value.strip()
                if url.startswith(("http://", "https://")):
                    self.urls.append(url)
                break


def extract_detail_image_urls(notes: Any) -> list[str]:
    """从采集箱的详情 HTML 里按顺序取图片 URL。"""
    if not isinstance(notes, str) or not notes:
        return []
    parser = _DetailImageParser()
    try:
        parser.feed(notes)
        parser.close()
    except Exception:  # noqa: BLE001 - 残缺 HTML 不该炸掉整次入仓
        return parser.urls
    return parser.urls


def detail_images_only(urls: Iterable[str]) -> str:
    """生成只含图片的紧凑详情 HTML。"""
    from html import escape

    return "".join(f'<img src="{escape(url, quote=True)}">' for url in urls)


def _is_price_field(key: Any) -> bool:
    return "price" in str(key).casefold()


def _price_fields(value: Any, path: str = "") -> dict[str, Any]:
    """收集所有价格字段的路径 -> 值（含嵌套 skuMap）。"""
    fields: dict[str, Any] = {}
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child = f"{path}.{key}" if path else str(key)
            if _is_price_field(key):
                fields[child] = deepcopy(nested)
            else:
                fields.update(_price_fields(nested, child))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            fields.update(_price_fields(nested, f"{path}[{index}]"))
    return fields


def prices_unchanged(original: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """源文与成品之间所有价格字段是否逐字相同（被删掉的 SKU 不算变化）。"""
    before = _price_fields(original)
    after = _price_fields(payload)
    sku_map = payload.get("skuMap") or {}
    for path, value in before.items():
        if path.startswith("skuMap."):
            sku_key = path.split(".", 2)[1]
            if sku_key not in sku_map:
                continue
        if path not in after or after[path] != value:
            return False
    for path in after.keys() - before.keys():
        if path.startswith("skuMap."):
            sku_key = path.split(".", 2)[1]
            if sku_key not in (original.get("skuMap") or {}):
                continue
        return False
    return True


def _decimal_or_none(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return value


def pricing_preserves_origin_price(original: Mapping[str, Any],
                                   payload: Mapping[str, Any]) -> bool:
    """采购价是记录，不是可调参数：定价只允许动 price。"""
    before = original.get("skuMap") or {}
    after = payload.get("skuMap") or {}
    for key, sku in before.items():
        if not isinstance(sku, Mapping):
            continue
        source = sku.get("originPrice")
        if key not in after:
            # 只有源标记删除的 SKU 才允许消失
            if str(sku.get("isDelete", "0")) != "1":
                return False
            continue
        target = after.get(key)
        if not isinstance(target, Mapping):
            return False
        if source in (None, "") and target.get("originPrice") in (None, ""):
            continue
        if _decimal_or_none(source) != _decimal_or_none(target.get("originPrice")):
            return False
    for _key in after.keys() - before.keys():
        return False
    return True


def _ci(payload: Mapping[str, Any], key: str) -> Any:
    """payload 里字段名大小写不统一，按不区分大小写取值。"""
    if key in payload:
        return payload.get(key)
    lowered = key.casefold()
    for name, value in payload.items():
        if str(name).casefold() == lowered:
            return value
    return None


def pre_save_issues(
    original: Mapping[str, Any],
    payload: Mapping[str, Any],
    coverage: Mapping[str, Any] | None,
    *,
    main_limit: int = 9,
    detail_limit: int = 30,
    package_dimensions: Mapping[str, Any] | None = None,
    size_chart_url: str = "",
    process_images_enabled: bool = True,
    image_only: bool = False,
    pricing_enabled: bool = False,
    title_review_words: Iterable[str] | None = None,
    image_review_pending: Any = None,
    requirements: Mapping[str, Any] | None = None,
) -> list[str]:
    """返回阻断保存的问题清单；空列表 = 可以入库。

    `title_review_words` / `image_review_pending` 是后加的两个**可选**入参，
    默认 None 时规则 13/14 完全不参与判定，老调用方的结果一字不变：
      · title_review_words: 标题命中的 review 类词列表；None = 不查
      · image_review_pending: `gate_service.main_image_gate` 的结果（或任何含
        blocked / needsReview / removeIndexes 的映射）；None = 不查

    `requirements` 同样是可选入参（`category_service.extract_requirements`
    的返回值，或直接给上游 category_metadata 原始响应）：None = 不查规则 11/12。
    为什么不在这里自己取上游：本模块是纯函数，被入仓 / 批量预览 / 只读检查
    三处调用，一旦自己发请求，预览也会被拖慢、单测也跑不动。
    """
    issues: list[str] = []
    coverage = coverage or {}

    # 1. 译文必须完整：只看「翻译成功」会漏掉嵌套字段里的中文
    if coverage.get("complete") is not True or coverage.get("missingFields"):
        issues.append("translation coverage is incomplete")

    # 2. 类目 cid 必须是正整数
    cid = str(_ci(payload, "cid") or "").strip()
    if not cid.isdigit() or int(cid) <= 0:
        issues.append("category cid is missing")

    if image_only:
        # 只处理图片时，非图片字段必须一字未动
        for key in ("packageLength", "packageWidth", "packageHeight"):
            value = _ci(payload, key)
            if value in (None, ""):
                issues.append("existing package dimensions are invalid")
                break
        if _non_image_data(original) != _non_image_data(payload):
            issues.append("non-image fields changed during image-only processing")
    else:
        # 3. 包裹尺寸必须与本次设置一致，否则运费按错的重量算
        expected = package_dimensions or {}
        if expected:
            for key, want in expected.items():
                if _ci(payload, key) != want:
                    issues.append("package dimensions do not match the selected settings")
                    break
        # 4. 尺码图必须是本次选中的那张
        if size_chart_url and _ci(payload, "sizeChart") != size_chart_url:
            issues.append("selected size chart is missing")

    # 5. 主图必须有，且不超本地设置上限
    main_images = _ci(payload, "imgUrls")
    if not isinstance(main_images, list) or not main_images:
        issues.append("at least one main image is required")
    elif process_images_enabled and len(main_images) > main_limit:
        issues.append("main image count exceeds its configured limit")

    # 6. 详情图超平台硬上限时，无论本地设置如何都会被平台拒
    detail_urls = extract_detail_image_urls(_ci(payload, "notes"))
    if len(detail_urls) > _DETAIL_HARD_LIMIT:
        issues.append(
            f"detail image count {len(detail_urls)} exceeds the platform limit "
            f"of {_DETAIL_HARD_LIMIT}")

    if process_images_enabled:
        # 7. 详情图不超本地设置，且详情区只能有图片
        if len(detail_urls) > detail_limit:
            issues.append("detail image count exceeds its configured limit")
        if _ci(payload, "notes") not in (None, "") and \
                _ci(payload, "notes") != detail_images_only(detail_urls):
            issues.append("detail contains text or non-image layout")
    else:
        # 8. 关掉图片处理却改了图片字段 —— 有别的流程动了不该动的字段
        original_main = _ci(original, "imgUrls")
        original_detail = extract_detail_image_urls(_ci(original, "notes"))
        if (main_images != original_main
                or detail_urls != original_detail
                or not _retained_sku_images_unchanged(original, payload)):
            issues.append("image fields changed while image processing is disabled")

    # 9/10. 价格：没开定价就不许变；开了定价也不许动 originPrice
    if not prices_unchanged(original, payload):
        if not pricing_enabled:
            issues.append("one or more price fields changed")
        elif not pricing_preserves_origin_price(original, payload):
            issues.append("pricing must not change any SKU originPrice")

    # 11/12. 类目自身的要求（老版 shoe_workflow.validate_category_requirements
    # :160-180）。前 10 条只判断商品自己；类目可能额外要求包裹长宽高，或要求
    # 某些属性必填，不满足上架就被平台拒。requirements 为 None 时整段跳过，
    # 老调用方行为不变。
    if requirements is not None:
        from services.category_service import validate_requirements

        issues.extend(validate_requirements(payload, requirements))

    # 13. 标题命中需复核词：老版在这里直接抛错停止保存。
    # 判定不在本模块做——调用方（gate_service.title_review_gate）已经跑过
    # transform_service 的清理逻辑，这里只消费结果，避免两套匹配规则各说各话。
    if title_review_words is not None:
        words = [str(word) for word in title_review_words if str(word)]
        if words:
            issues.append("title contains words that require review: "
                          + ", ".join(words))

    # 14. 主图 OCR 待复核 / 全部被判为删图：老版 review_main_images 在这里抛错。
    # 只认 blocked，不看 allow_review——放行是调用方的决定，它决定不传这一项；
    # 传了结果却要求「放行也算问题」会让同一次判定出现两种结论。
    if image_review_pending is not None:
        gate = image_review_pending if isinstance(image_review_pending, Mapping) else {}
        if gate.get("blocked"):
            message = str(gate.get("message") or "main image classification needs review")
            issues.append(f"main image classification needs review: {message}")

    return issues


def _retained_sku_images_unchanged(original: Mapping[str, Any],
                                   payload: Mapping[str, Any]) -> bool:
    """关掉图片处理时，保留下来的 SKU 图片必须一字未动。

    颜色类属性允许删值（平台侧合并同色），其它属性只许减不许改。
    """
    before_props = original.get("skuPropertyList") or []
    after_props = payload.get("skuPropertyList") or []
    if not isinstance(before_props, list) or not isinstance(after_props, list):
        return False
    if len(before_props) != len(after_props):
        return False
    for before_prop, after_prop in zip(before_props, after_props, strict=True):
        if not isinstance(before_prop, Mapping) or not isinstance(after_prop, Mapping):
            return False
        before_values = before_prop.get("attrValueList") or []
        after_values = after_prop.get("attrValueList") or []
        if not isinstance(before_values, list) or not isinstance(after_values, list):
            return False
        before_ids = [v.get("attrValueId") for v in before_values if isinstance(v, Mapping)]
        after_ids = [v.get("attrValueId") for v in after_values if isinstance(v, Mapping)]
        if len(before_ids) != len(before_values) or len(after_ids) != len(after_values):
            return False
        if (len(after_values) < len(before_values)
                and str(before_prop.get("attrName", "")).casefold()
                not in {"颜色", "色彩", "color", "colour", "warna"}):
            return False
        if (all(v not in (None, "") for v in before_ids + after_ids)
                and len(set(before_ids)) == len(before_ids)):
            retained = set(after_ids)
            if len(retained) != len(after_ids):
                return False
            if after_ids != [v for v in before_ids if v in retained]:
                return False
            before_by_id = dict(zip(before_ids, before_values, strict=True))
            pairs = [(before_by_id[v], after_v)
                     for v, after_v in zip(after_ids, after_values, strict=True)]
        else:
            if len(before_values) != len(after_values):
                return False
            pairs = list(zip(before_values, after_values, strict=True))
        for before_value, after_value in pairs:
            if not isinstance(before_value, Mapping) or not isinstance(after_value, Mapping):
                return False
            for field in ("imgUrl", "supplementarySkuImageUrls"):
                if before_value.get(field) != after_value.get(field):
                    return False
    return True


_IMAGE_KEYS = {"imgurls", "notes", "sizechart"}


def _non_image_data(payload: Mapping[str, Any]) -> dict[str, Any]:
    """去掉图片相关字段后的快照，用于「只处理图片时非图片字段不许变」的比对。"""
    snapshot: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        lowered = str(key).casefold()
        if lowered in _IMAGE_KEYS:
            continue
        if lowered == "skupropertylist" and isinstance(value, list):
            snapshot[key] = _strip_sku_images(value)
            continue
        snapshot[key] = value
    return snapshot


def _strip_sku_images(props: list[Any]) -> list[Any]:
    """SKU 属性里可能嵌图片 URL，比对非图片数据时忽略它们。"""
    cleaned: list[Any] = []
    for prop in props:
        if not isinstance(prop, Mapping):
            cleaned.append(prop)
            continue
        body = {}
        for key, value in prop.items():
            if _IMG_TAG.search(str(key)) or str(key).casefold() in {"imgurl", "img", "image"}:
                continue
            if isinstance(value, str) and _IMG_SRC.search(value):
                body[key] = _IMG_SRC.sub("", value)
            else:
                body[key] = value
        cleaned.append(body)
    return cleaned
