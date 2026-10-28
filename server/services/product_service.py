"""商品解析：妙手采集箱详情 → Product 记录。

硬规则（docs/api.md / architecture.md 3.6，不可回归）：
  · 主图上限 MAIN_IMAGE_LIMIT = 9，入库时截断；
  · 详情图软限 9、平台硬限 30，硬限必须在入库前校验；
  · 采购价取 SKU originPrice 的 min/max，币种默认 CNY；
  · 有效 SKU 只算 isDelete != "1" 的；
  · 商品身份 = (account_id, detailId, shopId)，仓库键 = `{detailId}:{shopId}`。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.constants import (
    DETAIL_IMAGE_HARD_LIMIT,
    MAIN_IMAGE_LIMIT,
    TITLE_MAX_LENGTH,
    TITLE_MIN_LENGTH,
)
from core.utils import (
    detail_image_urls,
    first_text,
    image_url,
    image_urls,
    to_decimal,
    to_int,
)
from models.product import Product
from services import price_template

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


# --- 上游结构 -------------------------------------------------------------
def detail_block(response: Mapping[str, Any], mode: str = "shop") -> dict[str, Any]:
    data = (response or {}).get("data") or {}
    key = "siteCollectItemInfo" if mode == "site" else "shopCollectItemInfo"
    # 翻译接口把结果放在 data.collectItemInfo，采集箱详情放在 data.shopCollectItemInfo。
    # 只认后两者的话，译文整个被丢掉：title 拿不到、sku 属性也拿不到，
    # 后果是覆盖检查永远报「skuPropertyList 缺失」，所有商品都存不进去。
    info = (data.get(key) or data.get("collectItemInfo") or {})
    return {
        "info": info if isinstance(info, dict) else {},
        "ossMd5": str(data.get("ossMd5") or ""),
        "editModel": str(data.get("editModel") or ""),
    }


def active_skus(info: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    sku_map = info.get("skuMap") or {}
    if not isinstance(sku_map, Mapping):
        return {}
    return {str(key): value for key, value in sku_map.items()
            if isinstance(value, Mapping) and str(value.get("isDelete", "0")) != "1"}


def purchase_range(info: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """采购价区间：有效 SKU 的 originPrice 最小 / 最大值（缺失回退 price）。"""
    values: list[float] = []
    for sku in active_skus(info).values():
        for field in ("originPrice", "price"):
            number = to_decimal(sku.get(field))
            if number is not None and number > 0:
                values.append(float(number))
                break
    if not values:
        return None, None
    return min(values), max(values)


def total_stock(info: Mapping[str, Any]) -> int:
    total = 0
    for sku in active_skus(info).values():
        value = to_int(sku.get("stock"))
        if value and value > 0:
            total += value
    return total


def sku_rows(info: Mapping[str, Any], *, limit: int = 200) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, sku in active_skus(info).items():
        rows.append({
            "skuKey": key,
            "price": str(sku.get("price") if sku.get("price") is not None else ""),
            "priceIncludeVat": (str(sku.get("priceIncludeVat"))
                                if sku.get("priceIncludeVat") is not None else None),
            "originPrice": (str(sku.get("originPrice"))
                            if sku.get("originPrice") is not None else None),
            "stock": to_int(sku.get("stock")) or 0,
            "weight": str(sku.get("weight") if sku.get("weight") is not None else ""),
            "isDelete": str(sku.get("isDelete") or "0"),
        })
        if len(rows) >= limit:
            break
    return rows


def sku_properties(info: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for prop in info.get("skuPropertyList") or []:
        if not isinstance(prop, Mapping):
            continue
        values = []
        for value in prop.get("attrValueList") or []:
            if not isinstance(value, Mapping):
                continue
            values.append({
                "attrValueId": str(value.get("attrValueId") or ""),
                "attrValue": str(value.get("attrValue") or ""),
                "imgUrl": image_url(value.get("imgUrl")) or None,
            })
        result.append({"attrName": str(prop.get("attrName") or ""),
                       "attrValueList": values})
    return result


def main_images(info: Mapping[str, Any], *, limit: int = MAIN_IMAGE_LIMIT) -> list[str]:
    return image_urls(info.get("imgUrls"), limit=limit)


def all_main_images(info: Mapping[str, Any]) -> list[str]:
    return image_urls(info.get("imgUrls"))


def detail_images(info: Mapping[str, Any]) -> list[str]:
    return detail_image_urls(info.get("notes"), limit=DETAIL_IMAGE_HARD_LIMIT)


def validate_image_limits(info: Mapping[str, Any]) -> None:
    """详情图超过平台硬限 30 张时拒绝（关闭图片处理也要校验）。"""
    urls = detail_image_urls(info.get("notes"))
    if len(urls) > DETAIL_IMAGE_HARD_LIMIT:
        raise errors.AppError(
            "DETAIL_IMAGE_LIMIT_EXCEEDED",
            f"详情图 {len(urls)} 张，超过平台硬限 {DETAIL_IMAGE_HARD_LIMIT} 张，请先删图",
            status=422,
            details={"count": len(urls), "limit": DETAIL_IMAGE_HARD_LIMIT},
        )


# --- 诊断 -----------------------------------------------------------------
def diagnose(info: Mapping[str, Any]) -> list[dict[str, str]]:
    """12 项检查（缺一不可：标题/描述/图/类目/重量/包裹尺寸/SKU/价格/
    含税价/库存/SKU 重量/标题长度）。"""
    issues: list[dict[str, str]] = []

    def check(condition: bool, code: str, message: str,
              severity: str = SEVERITY_ERROR) -> None:
        if condition:
            issues.append({"severity": severity, "code": code, "message": message})

    def number(value: Any) -> float:
        parsed = to_decimal(value)
        return float(parsed) if parsed is not None else 0.0

    title = first_text(info.get("title"))
    check(not title, "missing_title", "标题为空")
    if title and not (TITLE_MIN_LENGTH <= len(title) <= TITLE_MAX_LENGTH):
        check(True, "title_length",
              f"标题长度为 {len(title)}，不符合 {TITLE_MIN_LENGTH} 到 {TITLE_MAX_LENGTH} 字符",
              SEVERITY_WARNING)
    check(not info.get("notes"), "missing_notes", "商品描述为空")
    check(not all_main_images(info), "missing_images", "商品图片为空")
    check(not str(info.get("cid") or "").strip(), "missing_category", "类目为空")
    check(not info.get("weight") or number(info.get("weight")) <= 0,
          "missing_weight", "商品重量未填写或不大于 0")
    for field in ("packageLength", "packageWidth", "packageHeight"):
        check(not info.get(field) or number(info.get(field)) <= 0,
              f"missing_{field}", f"包裹尺寸字段 {field} 未填写或不大于 0")
    sku_map = info.get("skuMap") or {}
    check(not sku_map, "missing_sku", "SKU 为空")
    for sku_key, sku in (sku_map.items() if isinstance(sku_map, Mapping) else []):
        if not isinstance(sku, Mapping) or str(sku.get("isDelete", "0")) == "1":
            continue
        check(number(sku.get("price")) <= 0, "invalid_sku_price",
              f"SKU {sku_key} 价格不大于 0")
        if sku.get("priceIncludeVat") not in (None, ""):
            check(number(sku.get("priceIncludeVat")) <= 0, "invalid_sku_price_include_vat",
                  f"SKU {sku_key} 含税价不大于 0")
        check(number(sku.get("stock")) < 0, "invalid_sku_stock", f"SKU {sku_key} 库存小于 0")
        check(number(sku.get("weight")) <= 0, "missing_sku_weight",
              f"SKU {sku_key} 重量未填写或不大于 0")
    return issues


def detail_summary(response: Mapping[str, Any], *, detail_id: Any, shop_id: Any,
                   mode: str = "shop", category_path: str = "") -> dict[str, Any]:
    """GET /collect-box/{detailId} 的响应体（不含 payload 全量）。"""
    block = detail_block(response, mode)
    info = block["info"]
    if not info:
        raise errors.not_found(f"上游查无明细 {detail_id}（店铺 {shop_id}）",
                               detailId=str(detail_id), shopId=str(shop_id))
    if not block["ossMd5"]:
        raise errors.unprocessable("商品详情缺少 ossMd5，可能是妙手侧草稿不完整")
    expected_detail = info.get("detailId")
    if expected_detail not in (None, "") and str(expected_detail) != str(detail_id):
        raise errors.unprocessable("商品详情身份字段不一致（detailId）",
                                  expected=str(detail_id), actual=str(expected_detail))
    expected_shop = info.get("shopId")
    if mode == "shop" and expected_shop not in (None, "") and str(expected_shop) != str(shop_id):
        raise errors.unprocessable("商品详情身份字段不一致（shopId）",
                                  expected=str(shop_id), actual=str(expected_shop))

    images = main_images(info)
    details = detail_images(info)
    price_min, price_max = purchase_range(info)
    sku_count = len(active_skus(info))
    notes = info.get("notes")
    diagnostics = diagnose(info)
    ready = not any(item["severity"] == SEVERITY_ERROR for item in diagnostics)
    return {
        "detailId": str(detail_id),
        "shopId": str(shop_id),
        "mode": mode,
        "ossMd5": block["ossMd5"],
        "editModel": block["editModel"] or "shop",
        "title": first_text(info.get("title")),
        "site": str(block["info"].get("site") or ""),
        "cid": str(info.get("cid") or ""),
        "categoryPath": category_path,
        "language": str(info.get("language") or ""),
        "images": images,
        "imageCount": len(all_main_images(info)),
        "detailImages": details,
        "skuCount": sku_count,
        "skuProperties": sku_properties(info),
        "skus": sku_rows(info),
        "purchasePrice": {
            "min": _money(price_min),
            "max": _money(price_max),
            "currency": "CNY",
        },
        "weight": _text_or_none(info.get("weight")),
        "packageDimensions": {
            "length": _num_or_none(info.get("packageLength")),
            "width": _num_or_none(info.get("packageWidth")),
            "height": _num_or_none(info.get("packageHeight")),
        },
        "sizeChartUrl": first_text(info.get("sizeChart")) or None,
        "notes": notes if isinstance(notes, str) else "",
        "diagnostics": diagnostics,
        "readyToProcess": ready,
        "mainImageLimit": MAIN_IMAGE_LIMIT,
        "detailImageLimit": DETAIL_IMAGE_HARD_LIMIT,
    }


def _money(value: float | None) -> Any:
    if value is None:
        return None
    from core.utils import money as money_str

    return money_str(value)


def _text_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _num_or_none(value: Any) -> float | None:
    number = to_decimal(value)
    return float(number) if number is not None else None


# --- 入库 -----------------------------------------------------------------
def build_product(*, info: Mapping[str, Any], detail_id: Any, shop_id: Any,
                  account_id: int | None, shop_name: str = "", language: str = "",
                  category_id: int | None = None, category_name: str = "",
                  title: str = "", site: str = "") -> dict[str, Any]:
    """把 finish 后的 payload 变成 Product 字段字典（不含 ORM 会话操作）。"""
    validate_image_limits(info)
    images = main_images(info)
    details = detail_images(info)
    price_min, price_max = purchase_range(info)
    cid = str(info.get("cid") or "").strip()
    resolved_title = first_text(title, info.get("title"))
    return {
        "detail_id": str(detail_id),
        "shop_id": str(shop_id),
        "shop_name": shop_name,
        "account_id": account_id,
        "title": resolved_title[:TITLE_MAX_LENGTH],
        "language": language or str(info.get("language") or ""),
        "site": site or str(info.get("site") or ""),
        "cover": images[0] if images else (image_url(info.get("mainImgUrl")) or ""),
        "cid": cid,
        "category_id": category_id,
        "category_name": category_name,
        "images": images,
        "detail_images": details,
        "sku_properties": sku_properties(info),
        "sku_count": len(active_skus(info)),
        "price_min": price_min,
        "price_max": price_max,
        "price_currency": "CNY",
        "stock": total_stock(info),
        "payload": dict(info),
    }


def upsert_product(db: Session, account_id: int | None, fields: dict[str, Any], *,
                   warehouse_key: str) -> Product:
    """按 (account_id, detailId, shopId) upsert；重复保存保留认领状态与分组。"""
    query = db.query(Product).filter(Product.warehouse_key == warehouse_key)
    product = query.one_or_none()
    if product is None:
        product = Product(warehouse_key=warehouse_key)
        db.add(product)
    else:
        # 命中已软删的那一行时必须先复活：删除是软删（deleted_at + status=archived），
        # 而这里按 warehouse_key 查是不带 deleted_at 条件的。如果不显式清掉，
        # 删掉商品再重新处理同一个 detailId:shopId 会返回 200、字段也都写进去了，
        # 但列表和详情都被 active_query 过滤掉——看起来就是「处理成功却找不到」。
        product.deleted_at = None
        if product.status in (None, "", "archived"):
            product.status = "ready"
    for key, value in fields.items():
        setattr(product, key, value)
    product.warehouse_key = warehouse_key
    from core.utils import now_ts

    product.updated_at = now_ts()
    if product.saved_at is None:
        product.saved_at = now_ts()
    if not product.status:
        product.status = "ready"
    # 乐观锁版本号：入仓/重复入仓都会整份覆盖 payload，是最典型的「静默覆盖」路径
    # （A 在改标题，B 重新入仓同一件，A 保存时把整份 payload 写回旧内容）。
    # 这里推进版本号，让 A 手上那份过期数据在下一次保存时被 409 拦住。
    product.revision = int(product.revision or 0) + 1
    db.flush()
    return product


def process_price(payload: Mapping[str, Any], *, template: Mapping[str, Any] | None,
                  exchange_rate: Any = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """按定价模板给 payload 重算售价（模板为空时原样返回）。"""
    if not template:
        return dict(payload), None
    try:
        priced, report = price_template.apply_template(payload, template, rate=exchange_rate)
    except price_template.PriceTemplateError as exc:
        raise errors.AppError(errors.TEMPLATE_INVALID,
                              f"定价模板不可用：{exc}", status=422,
                              details={"field": "template"}) from None
    return priced, report


def category_label(info: Mapping[str, Any]) -> str:
    """类目展示名：优先平台中文名，其次英文名，最后 cid。"""
    name_cn = first_text(info.get("categoryNameChinese"), info.get("cidNameCn"))
    name = first_text(info.get("categoryName"), info.get("cidName"))
    cid = str(info.get("cid") or "").strip()
    return name_cn or name or (f"类目 {cid}" if cid else "未分类")


# 目标语言默认马来语（与老版一致）；妙手支持 ms/en/th/vi/zh
DEFAULT_LANGUAGE = "ms"


def translated_info(client, info: Mapping[str, Any], language: str = ""
                    ) -> tuple[dict[str, Any], dict[str, Any]]:
    """把商品文案翻译成目标语言（标题、描述、SKU 属性）。

    老版的核心步骤：1688 的标题是中文，直接上架等于没做本地化。翻译失败时
    保留原文并回报原因，不阻断入仓（少翻一次可以补，卡住整批不行）。
    """
    target = str(language or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE
    try:
        response = client.translate_collect_item(dict(info), target_language=target,
                                                 translate_sku_property=True)
    except Exception as exc:  # noqa: BLE001 - 翻译失败不应阻断入仓
        return dict(info), {"translated": False, "language": target,
                            "error": f"{type(exc).__name__}: {exc}"[:160]}
    block = detail_block(response, mode="shop")
    merged = dict(info)
    for key in ("title", "description", "notes"):
        value = block["info"].get(key)
        if value:
            merged[key] = value
    # SKU 属性名与值（颜色/尺码）也要跟着翻
    props = block["info"].get("skuPropertyList")
    if props:
        merged["skuPropertyList"] = props
    # 商品属性（产地/性别/材质…）同理。上游翻了但这里不合并，
    # 覆盖检查就会报 productAttributes 缺失，把商品挡在保存之外。
    attributes = block["info"].get("productAttributes")
    if attributes:
        merged["productAttributes"] = attributes
    # 卖点短句同样是可翻译文本
    features = block["info"].get("keyProductFeatures")
    if features:
        merged["keyProductFeatures"] = features
    changed = sum(1 for k in ("title", "description") if merged.get(k) != info.get(k))
    return merged, {"translated": True, "language": target, "changedFields": changed}


# 中文颜色词表（与老版 shoe_workflow.COLOR_NAMES 对齐，含深浅前缀）
_COLOR_NAMES = (
    "咖啡", "卡其", "藏青", "米白", "乳白", "玫红", "酒红", "天蓝", "宝蓝", "墨绿",
    "军绿", "杏", "棕", "褐", "驼", "青", "黑", "白", "灰", "红", "橙", "黄",
    "绿", "蓝", "紫", "粉", "金", "银", "米",
)
_COLOR_TOKEN = re.compile(r"(?:浅|深|暗|亮|荧光)?(?:" + "|".join(_COLOR_NAMES) + r")色?")
_EN_COLORS = (
    "black", "white", "beige", "grey", "gray", "apricot", "nude", "pink", "red",
    "blue", "green", "brown", "khaki", "navy", "purple", "yellow", "orange",
    "silver", "gold", "cream", "coffee", "wine", "rose", "leopard", "camel",
    "ivory", "burgundy", "olive", "tan", "multicolor",
)
# 马来语颜色词（翻译后商品的属性值大量是马来语，审查 P2-5）
_MY_COLORS = (
    "hitam", "putih", "kelabu", "kelabu", "merah", "biru", "hijau", "kuning",
    "ungu", "oren", "jingga", "emas", "perak", "perang", "coklat", "krem",
    "merah jambu", "biru muda", "hijau muda",
)
_ALL_COLORS = tuple(dict.fromkeys(_EN_COLORS + _MY_COLORS))
_COLOR_ALT = "|".join(sorted(_ALL_COLORS, key=len, reverse=True))
_MODIFIER_ALT = "dark|light|deep|pale|bright|pastel|gelap|cerah|muda|tua"
_EN_COLOR_RE = re.compile(
    r"\b(?:(?P<mod>" + _MODIFIER_ALT + r")\s+)?(?P<color>" + _COLOR_ALT + r")\b", re.I)
# 无空格拼接词（Ankle-lengthpantsblack / Nine-pointstylenordicgray）：
# 颜色词出现在词尾，无法用 \b 命中，单独用后缀匹配兜底
_SUFFIX_COLOR_RE = re.compile(r"(" + _COLOR_ALT + r")$", re.I)


def clean_color(value: Any) -> str:
    """把供应商写的颜色收敛成可读名称。

    供应商导出常见 `Y18[beige]`、`Beige + Black [Two-Piece Set]`、`1987白色`
    这类写法。取不到颜色时返回原文（不抛错），避免整批卡在一个奇怪的颜色上。
    """
    original = str(value or "").strip()
    if not original:
        return ""
    text = re.sub(r"\s+", "", original)
    found_cn = _COLOR_TOKEN.findall(text)
    if found_cn:
        return "".join(dict.fromkeys(found_cn))
    found_en = list(_EN_COLOR_RE.finditer(original))
    if found_en:
        names: list[str] = []
        for match in found_en:
            color = match.group("color").capitalize()
            if match.group("mod"):
                color = f"{match.group('mod').capitalize()} {color}"
            if color not in names:
                names.append(color)
        return " ".join(names)[:40]
    suffix = _SUFFIX_COLOR_RE.search(original)
    if suffix:
        return suffix.group(1).capitalize()
    # 型号 / 编码 / 括号备注：原样返回，避免把 ABC123 改成 Abc123
    if re.search(r"\d", original) or re.search(r"[\[\]()（）]", original):
        return original[:40]
    if re.search(r"[A-Za-z]", original):
        return " ".join(w.capitalize() for w in original.split())[:40]
    return original[:40]


def clean_color_strict(value: Any) -> str:
    """严格版颜色清洗：认不出就抛 `color_service.ColorError`（审查 P0-3）。

    和上面的 `clean_color` 是刻意并存的两条路，不能合并：
      · `clean_color` 是「尽力抽取」，取不到就原样返回。它服务于展示类场景
        （列表页文案），宁可有噪音也不能让一行数据消失；
      · `clean_color_strict` 是「认不出就停」，服务于入仓/上架。颜色错了
        会在平台侧被拒，或者更糟——两个不同颜色被合并成一个 SKU 卖出去。
    实现在 `services/color_service.py`，这里只做转发：颜色常量与正则集中在
    那个模块，`warehouse_service` 下一轮接线时不必反向依赖 product_service。
    """
    from services.color_service import clean_color_strict as _strict

    return _strict(value)


def scan_violation_words(db: Session, text: str) -> list[dict[str, str]]:
    """按词库扫描标题/描述，返回命中项（供入仓时提示）。"""
    from services import word_service

    if not text:
        return []
    hits: list[dict[str, str]] = []
    lowered = text.casefold()
    for word, action in word_service.matcher(db):
        if word and word.casefold() in lowered:
            hits.append({"word": word, "action": action})
    return hits
