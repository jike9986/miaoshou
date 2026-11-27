"""草稿处理标记的读写：查、置位、失效。

标记必须**独立事务提交**，这是本模块存在的全部理由。
`save_from_collect_box` 的调用方（`api/collect_box.py`）用的是
`get_db()`，它 `yield` 之后才 commit，中途抛异常就整体 rollback。
文字处理在写回草稿那一步已经真的完成（妙手那边草稿已经改了），
若把标记写进同一个事务，读回核验一失败标记就跟着蒸发 ——
下次重新处理又拿中文规则去洗已经是马来语的草稿，颜色被删，
读回再次不一致，永远出不来。2026-11-27 的 6 件商品就是这么卡死的。

所以这里所有写操作都用**自己的 Session** 并立刻 commit，
与调用方事务完全解耦。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from core.utils import now_ts
from db.session import SessionLocal
from models.draft_mark import DraftMark

logger = logging.getLogger(__name__)


def _norm_shop(shop_id: Any) -> str:
    return str(shop_id or "").strip()


def get_mark(db: Session, *, account_id: int, detail_id: str,
             shop_id: Any) -> DraftMark | None:
    """读标记。只读，用调用方的 session 即可。"""
    return (db.query(DraftMark)
            .filter(DraftMark.account_id == int(account_id),
                    DraftMark.detail_id == str(detail_id),
                    DraftMark.shop_id == _norm_shop(shop_id))
            .one_or_none())


def text_already_done(db: Session, *, account_id: int, detail_id: str,
                      shop_id: Any, language: str) -> bool:
    """文字处理过没有。语言换了就要重做，所以语言也必须一致。

    语言为空（不翻译）时只认 `text_language` 同样为空的那次记录 ——
    「中文处理过」不能拿来当「马来语也处理过」。
    """
    mark = get_mark(db, account_id=account_id, detail_id=detail_id,
                    shop_id=shop_id)
    if mark is None or not mark.text_done:
        return False
    want = str(language or "").strip().lower()
    have = str(mark.text_language or "").strip().lower()
    return want == have


def image_already_done(db: Session, *, account_id: int, detail_id: str,
                       shop_id: Any, expected_total: int | None = None) -> bool:
    """图片处理过没有。

    `expected_total` 给定时还要求张数一致：商品图换了（图床地址变了、
    上游补图）就不能拿旧标记挡路，否则新图永远翻不到。
    不传就只认「跑完过」这一件事。
    """
    mark = get_mark(db, account_id=account_id, detail_id=detail_id,
                    shop_id=shop_id)
    if mark is None or not mark.image_done:
        return False
    if expected_total is None:
        return True
    return int(mark.image_total or 0) == int(expected_total)


def _upsert(db: Session, *, account_id: int, detail_id: str,
            shop_id: Any) -> DraftMark:
    mark = get_mark(db, account_id=account_id, detail_id=detail_id,
                    shop_id=shop_id)
    if mark is None:
        mark = DraftMark(account_id=int(account_id),
                         detail_id=str(detail_id),
                         shop_id=_norm_shop(shop_id))
        db.add(mark)
        db.flush()
    return mark


def _commit_in_own_session(account_id: int, detail_id: str, shop_id: Any,
                           **fields: Any) -> None:
    """用独立 Session 写标记并立刻提交 —— 绝不参与调用方事务。

    失败只记日志，不向上抛：标记写不进去最多导致下次多处理一遍，
    比让整件商品处理失败轻得多。
    """
    own = SessionLocal()
    try:
        mark = _upsert(own, account_id=account_id, detail_id=detail_id,
                       shop_id=shop_id)
        for key, value in fields.items():
            setattr(mark, key, value)
        mark.updated_at = now_ts()
        own.commit()
        logger.info("草稿标记已写入 detailId=%s shop=%s %s",
                    detail_id, _norm_shop(shop_id),
                    ", ".join(f"{k}={v}" for k, v in fields.items()))
    except Exception as exc:  # noqa: BLE001 - 标记失败不该拖垮处理
        own.rollback()
        logger.warning("草稿标记写入失败 detailId=%s：%s", detail_id, exc)
    finally:
        own.close()


def mark_text_done(*, account_id: int, detail_id: str, shop_id: Any,
                   language: str) -> None:
    """文字清洗 + 翻译已写回妙手草稿。写完立刻置位。"""
    _commit_in_own_session(
        account_id, detail_id, shop_id,
        text_done=True, text_at=now_ts(),
        text_language=str(language or "").strip().lower())


def mark_image_done(*, account_id: int, detail_id: str, shop_id: Any,
                    total: int) -> None:
    """图片全部 ready 且已写回草稿。"""
    _commit_in_own_session(
        account_id, detail_id, shop_id,
        image_done=True, image_at=now_ts(), image_total=int(total or 0))


def clear_text(*, account_id: int, detail_id: str, shop_id: Any) -> None:
    """文字标记失效（例如运营手动改了语言，或需要强制重跑）。"""
    _commit_in_own_session(
        account_id, detail_id, shop_id,
        text_done=False, text_at=None, text_language="")


def clear_image(*, account_id: int, detail_id: str, shop_id: Any) -> None:
    """图片标记失效（商品换图后需要重翻）。"""
    _commit_in_own_session(
        account_id, detail_id, shop_id,
        image_done=False, image_at=None, image_total=0)
