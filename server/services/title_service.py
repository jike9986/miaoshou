"""标题清理预览与单标题保存（老版 `/api/save-title` 的等价实现）。

契约要点：

* **预览只算不改**：`preview()` 不写库、不调妙手，纯粹按本地违规词库跑一遍
  `word_service.matcher()` + `word_service.clean_title()`，保证与入仓清理用的是
  同一套规则（`warehouse_service.save_from_collect_box` 走的就是这两个函数），
  不在这里另写一份匹配逻辑。
* **保存只改本地**：`save()` 只写本机 SQLite 的 `products.title` /
  `products.payload.title`，**不回写妙手草稿**。新版没有「改标题同步上游」的
  语义，调用方不要指望妙手侧标题跟着变。
* `remove` 类违规词直接删；`review` 类只记录在 `reviewWords` 里，不参与删除。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.utils import as_id_str, now_ts
from services import warehouse_service, word_service

logger = logging.getLogger("aohuang.titles")

# 妙手文档写死的标题长度区间。下限 25 不是随手编的：
# 低于 25 字符的标题在平台上信息量不足，保存接口会拒。
# 之前这里是 1，等于没有下限，短标题到保存时才炸。
TITLE_MIN_LEN = 25
TITLE_MAX_LEN = 255
PREVIEW_DEFAULT_LIMIT = 50
PREVIEW_MAX_LIMIT = 200


# --- 查询（复用 warehouse_service 的既有实现，不重写一套） -------------------
def _find_active(db: Session, ident: Any):
    """按 id 或 warehouseKey 找商品；找不到 / 已软删除返回 None（预览用）。"""
    try:
        return warehouse_service.resolve_product(db, ident)
    except errors.AppError:
        return None


def require_product(db: Session, ident: Any):
    """按 id 或 warehouseKey 找商品；不存在或已软删除抛 404 NOT_FOUND。"""
    return warehouse_service.resolve_product(db, ident)


# --- 词库命中 -------------------------------------------------------------
def review_hits(title: str, words: Iterable[tuple[str, str]]) -> list[str]:
    """Compatibility response for the removed review-only category."""
    return []


# 近 N 年内的四位年份才当作「采集年份」处理。
# 更早的数字多半是型号，动了就毁了标题。
_YEAR_LOOKBACK = 8


def refresh_title_years(title: str, *, current_year: int | None = None) -> tuple[str, int]:
    """把过时的采集年份换成今年（老版 transforms.refresh_title_years）。

    1688 标题里常年挂着「2024新款」，跨年后照搬上架看着就旧。
    只动「今年往前 8 年内、且小于今年」的四位年份，
    型号（X5、A21）、尺码（42）这类数字不碰。
    """
    text = str(title or "")
    if not text:
        return text, 0
    year = current_year or datetime.now().year
    oldest = max(2000, year - _YEAR_LOOKBACK + 1)
    years = "|".join(str(oldest + i) for i in range(year - oldest))
    pattern = re.compile(rf"(?<!\d)({years})(?!\d)")
    replaced = 0

    def swap(match: re.Match[str]) -> str:
        nonlocal replaced
        replaced += 1
        return str(year)

    return pattern.sub(swap, text), replaced


def _preview_one(index: int, title: Any, words: list[tuple[str, str]],
                 refresh_years: bool = False) -> dict[str, Any]:
    original = str(title or "")
    cleaned, removed = word_service.clean_title(original, words)
    # 年份刷新默认关着：改标题是有风险的动作，必须显式要求才做。
    # 但即使不刷新，也把「能刷新几个年份」报出来，让人知道该不该开。
    year_hits = refresh_title_years(cleaned)[1]
    if refresh_years and year_hits:
        cleaned, _ = refresh_title_years(cleaned)
    return {
        "index": index,
        "original": original,
        "cleaned": cleaned,
        "removedWords": list(removed),
        "reviewWords": review_hits(original, words),
        "staleYears": year_hits,
        "changed": cleaned != original,
    }


# --- 预览 -----------------------------------------------------------------
def normalize_limit(limit: Any) -> int:
    """limit 默认 50、最大 200；非法值按默认处理，不因为一个数字整单失败。"""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return PREVIEW_DEFAULT_LIMIT
    if value < 1:
        return PREVIEW_DEFAULT_LIMIT
    return min(value, PREVIEW_MAX_LIMIT)


def preview(db: Session, *, titles: Any = None, warehouse_ids: Any = None,
            limit: Any = None, refresh_years: bool = False) -> dict[str, Any]:
    """标题清理预览：不写库、不调上游。

    `titles` 与 `warehouseIds` 二选一；都没有（或全是空串）→ 400 VALIDATION_ERROR。
    `warehouseIds` 里查不到的标识会被跳过（预览不该因为一个过期 id 整单失败）。
    """
    raw_titles = [str(item or "").strip() for item in (titles or [])
                  if str(item or "").strip()]
    raw_idents = [str(item or "").strip() for item in (warehouse_ids or [])
                  if str(item or "").strip()]
    if not raw_titles and not raw_idents:
        raise errors.validation(
            "请提供 titles 或 warehouseIds 其中之一（titles 优先）", field="titles")

    sources: list[str] = list(raw_titles)
    if not sources:
        # 走库：按 warehouseIds 取标题，顺序与入参一致，取不到的跳过
        for ident in raw_idents:
            product = _find_active(db, ident)
            if product is not None:
                sources.append(product.title or "")

    sources = sources[:normalize_limit(limit)]
    words = word_service.matcher(db)
    items = [_preview_one(index, title, words, refresh_years)
             for index, title in enumerate(sources)]
    changed = [item for item in items if item["changed"]]
    logger.info("标题清理预览 titles=%s warehouseIds=%s 命中改动词=%s",
                len(raw_titles), len(raw_idents), len(changed))
    return {"items": items, "total": len(items), "changedCount": len(changed)}


# --- 单标题保存 -----------------------------------------------------------
def _validate_title(text: str) -> str:
    if not TITLE_MIN_LEN <= len(text) <= TITLE_MAX_LEN:
        raise errors.validation(
            f"标题长度必须在 {TITLE_MIN_LEN} 到 {TITLE_MAX_LEN} 之间",
            field="title", value=len(text))
    return text


def save(db: Session, ident: Any, *, title: Any, clean: bool = False,
         auto_commit: bool = True) -> dict[str, Any]:
    """保存单个商品标题（**只改本地库，不回写妙手草稿**）。

    `clean=True` 时先按违规词库清理（remove 类直接删、review 类只记录）再落库。
    """
    product = require_product(db, ident)
    previous = str(product.title or "")
    words = word_service.matcher(db)

    text = _validate_title(str(title if title is not None else "").strip())
    removed: list[str] = []
    if clean:
        text, removed = word_service.clean_title(text, words)
        text = _validate_title(str(text or "").strip())

    if text != previous:
        product.title = text
        payload = dict(product.payload or {})
        payload["title"] = text
        product.payload = payload
        product.updated_at = now_ts()
        # 乐观锁：标题也是商品行的一部分，改完必须换版本号，
        # 否则另一个人手上那份旧版本号在标题改过之后仍然能通过校验。
        product.revision = int(product.revision or 0) + 1
        db.flush()
        if auto_commit:
            db.commit()
        logger.info("标题已保存（仅本地）id=%s changed=%s removed=%s",
                    product.id, True, removed)
    else:
        logger.info("标题已保存（仅本地）id=%s changed=%s removed=%s",
                    product.id, False, removed)

    return {
        "id": as_id_str(product.id),
        "detailId": product.detail_id,
        "shopId": product.shop_id or "",
        "title": str(product.title or ""),
        "previousTitle": previous,
        "changed": str(product.title or "") != previous,
        "removedWords": list(removed),
    }
