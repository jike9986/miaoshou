"""批量变换：按倍数改价 / 指定目标价 / 按序号或 URL 删图（老版 transforms.py 的移植）。

老版这块是运营最常用的部分——「统一打 1.35 倍」「指定 39.9」「删掉第 3 张和第 7
张带水印的图」都靠它。新版此前完全没有对应能力，只能整件按模板重算。

设计上和定价模板是两条路：
  · 定价模板 = 按成本、费率、利润算出该卖多少（重算）
  · 批量变换 = 在现有价上做缩放或直接指定（覆盖）
两者互斥使用，同时用会互相打架，所以 report 里分开列。

所有校验都放在动手之前：倍数与目标价不能同时给、含税价必须先有价格来源、
图片序号必须在范围内。校验不全就改了一半的数据，比直接报错难收拾得多。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from services import word_service
from services.title_service import refresh_title_years

logger = logging.getLogger(__name__)

# 报告里最多给几个改动样本；全量给出会让前端卡住，也没人看
SAMPLE_LIMIT = 10


class TransformError(ValueError):
    """变换参数不合法；消息直接给人看。"""


def _decimal(value: Any, label: str) -> Decimal:
    if value in (None, ""):
        raise TransformError(f"{label} 不能为空")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TransformError(f"{label} 不是有效数字：{value!r}") from None
    if not number.is_finite():
        raise TransformError(f"{label} 不是有效数字：{value!r}")
    return number


def _money(value: Decimal | None) -> float | None:
    """金额统一到两位小数（银行家舍入会让 0.005 飘，这里用四舍五入）。"""
    if value is None:
        return None
    return float(value.quantize(Decimal("0.01")))


def active_skus(item: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """所有未删除的 SKU；结构不对就报错，不猜。"""
    sku_map = item.get("skuMap") or {}
    if not isinstance(sku_map, dict):
        raise TransformError("skuMap 不是对象，无法安全改价")
    result: list[tuple[str, dict[str, Any]]] = []
    for key, sku in sku_map.items():
        if not isinstance(sku, dict):
            raise TransformError(f"SKU {key} 不是对象，无法安全改价")
        if str(sku.get("isDelete", "0")) != "1":
            result.append((str(key), sku))
    return result


def set_sku_prices(item: dict[str, Any], *, price_multiplier: Any = None,
                   target_price: Any = None,
                   target_price_include_vat: Any = None) -> dict[str, Any]:
    """按倍数或目标价改每个有效 SKU 的价格。

    倍数模式下同时缩放 priceIncludeVat，保持两个字段的比例关系；
    目标价模式下如果没显式给含税价，按原来的比例换算，不能凭空丢掉。
    """
    if price_multiplier is not None and target_price is not None:
        raise TransformError("倍数与目标价不能同时使用")
    if (target_price_include_vat is not None and target_price is None
            and price_multiplier is None):
        raise TransformError("指定含税价需要同时给出倍数或目标价")
    if price_multiplier is None and target_price is None:
        return {"changedCount": 0, "samples": []}

    active = active_skus(item)
    if not active:
        raise TransformError("没有可改价的有效 SKU")

    multiplier = (_decimal(price_multiplier, "倍数")
                  if price_multiplier is not None else None)
    if multiplier is not None and multiplier <= 0:
        raise TransformError("倍数必须大于 0")
    fixed_price = _decimal(target_price, "目标价") if target_price is not None else None
    fixed_include = (_decimal(target_price_include_vat, "目标含税价")
                     if target_price_include_vat is not None else None)
    if fixed_price is not None and fixed_price <= 0:
        raise TransformError("目标价必须大于 0")
    if fixed_include is not None and fixed_include <= 0:
        raise TransformError("目标含税价必须大于 0")

    samples: list[dict[str, Any]] = []
    changed_count = 0
    for sku_key, sku in active:
        old_price = _decimal(sku.get("price"), f"SKU {sku_key} 的售价")
        old_include = sku.get("priceIncludeVat")
        old_include_d = (_decimal(old_include, f"SKU {sku_key} 的含税价")
                         if old_include not in (None, "") else None)

        if multiplier is not None:
            new_price_d = old_price * multiplier
            new_include_d = old_include_d * multiplier if old_include_d is not None else None
        else:
            new_price_d = fixed_price
            if fixed_include is not None:
                new_include_d = fixed_include
            elif old_include_d is not None and old_price > 0:
                # 没给含税价就按原比例换算，别把含税价丢掉
                new_include_d = old_include_d * new_price_d / old_price
            else:
                new_include_d = None

        new_price = _money(new_price_d)
        new_include = _money(new_include_d) if new_include_d is not None else None
        include_changed = False
        if new_include is not None:
            include_changed = (old_include_d is None
                               or new_include != _money(old_include_d))
        changed = new_price != _money(old_price) or include_changed
        if changed:
            changed_count += 1
        sku["price"] = new_price
        if new_include is not None:
            sku["priceIncludeVat"] = new_include
        if len(samples) < SAMPLE_LIMIT:
            samples.append({
                "skuKey": sku_key,
                "priceBefore": float(old_price),
                "priceAfter": new_price,
                "priceIncludeVatBefore": (float(old_include_d)
                                          if old_include_d is not None else None),
                "priceIncludeVatAfter": new_include,
            })
    return {"changedCount": changed_count, "samples": samples}


def remove_images(item: dict[str, Any], *, remove_indexes: Iterable[int] = (),
                  remove_urls: Iterable[str] = ()) -> dict[str, Any]:
    """按 1 起算的序号或按 URL 删主图。"""
    images = item.get("imgUrls") or []
    if not isinstance(images, list):
        raise TransformError("imgUrls 不是数组，无法安全删图")
    indexes = {int(index) for index in remove_indexes}
    if any(index < 1 or index > len(images) for index in indexes):
        raise TransformError(f"图片序号必须在 1 到 {len(images)} 之间")
    urls = {str(url).strip() for url in remove_urls if str(url).strip()}
    removed: list[str] = []
    kept: list[Any] = []
    for position, url in enumerate(images, start=1):
        if position in indexes or str(url) in urls:
            removed.append(url)
        else:
            kept.append(url)
    if removed:
        item["imgUrls"] = kept
    return {"removedCount": len(removed), "removedUrls": removed,
            "remainingCount": len(kept)}


def transform_item(item: Mapping[str, Any], *, title: Any = None,
                   remove_title_words: Iterable[str] = (),
                   title_words: Mapping[str, list[str]] | None = None,
                   price_multiplier: Any = None, target_price: Any = None,
                   target_price_include_vat: Any = None,
                   remove_image_indexes: Iterable[int] = (),
                   remove_image_urls: Iterable[str] = ()
                   ) -> tuple[dict[str, Any], dict[str, Any]]:
    """对一件商品做变换，返回 (结果副本, 变更报告)。

    报告分四段：title / prices / images / warnings，外加 changed 汇总。
    前端只凭这份报告就能说清「改了什么、还有什么要看一眼」。
    """
    result = deepcopy(dict(item))
    explicit_words = tuple(remove_title_words)
    indexes = tuple(remove_image_indexes)
    urls = tuple(remove_image_urls)
    report: dict[str, Any] = {"title": None, "prices": None, "images": None,
                              "warnings": []}

    if title is not None or explicit_words or title_words is not None:
        source_title = (str(title).strip() if title is not None
                        else str(result.get("title") or ""))
        words_to_remove = list(title_words["remove"]) if title_words is not None else []
        if title is None or title_words is not None:
            words_to_remove.extend(explicit_words)
        # 词库走 SQLite（remove/review 动作），这里拼成 clean_title 要的形状
        pairs = [(str(word).strip(), "remove") for word in words_to_remove
                 if str(word).strip()]
        new_title, _removed = word_service.clean_title(source_title, pairs)
        new_title, years_fixed = refresh_title_years(new_title)
        if years_fixed:
            report["warnings"].append(f"标题年份已更新为今年（{years_fixed} 处）")
        if new_title != result.get("title"):
            report["title"] = {"before": result.get("title"), "after": new_title}
        result["title"] = new_title
        if title_words is not None:
            removed = [word for word in words_to_remove if str(word).strip()
                       and str(word).strip().casefold() in source_title.casefold()]
            review = [word for word in title_words["review"]
                      if str(word).casefold() in source_title.casefold()]
            length_ok = 25 <= len(new_title) <= 255
            report["titleCheck"] = {
                "before": source_title,
                "after": new_title,
                "removedWords": list(dict.fromkeys(removed)),
                "reviewWords": review,
                "needsReview": bool(review) or not length_ok,
            }
            if review:
                report["warnings"].append("标题命中需复核词：" + "、".join(review))
            if not length_ok:
                report["warnings"].append(
                    f"清理后标题长度为 {len(new_title)}，"
                    "不符合文档要求的 25 到 255 字符，请复核")

    if (price_multiplier is not None or target_price is not None
            or target_price_include_vat is not None):
        report["prices"] = set_sku_prices(
            result, price_multiplier=price_multiplier, target_price=target_price,
            target_price_include_vat=target_price_include_vat)

    if indexes or urls:
        report["images"] = remove_images(
            result, remove_indexes=indexes, remove_urls=urls)
        if report["images"]["removedCount"]:
            report["warnings"].append(
                "本地预览只修改 imgUrls；notes 中的详情 HTML 图片未自动改写")

    report["changed"] = bool(
        report["title"]
        or (report["prices"] and report["prices"].get("changedCount", 0) > 0)
        or (report["images"] and report["images"].get("removedCount", 0) > 0))
    return result, report
