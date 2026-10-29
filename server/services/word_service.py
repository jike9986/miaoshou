"""违规词库：新增 / 删除 / 批量导入 / 批量删除 / 清空。

上限 500 条（action=remove）；同一词不允许同时出现在 remove 与 review。
导入接受 JSON 数组或纯文本（老版标题词库的粘贴口径），超长/重复/超限的词
逐条跳过而不是整单报错——用户粘的是整张表，报错只让人反复手改。
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
import unicodedata
from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.constants import WORD_LIMIT
from core.utils import clean_text, now_ts
from models.violation_word import ViolationWord

logger = logging.getLogger(__name__)

ACTIONS = ("remove",)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# 单次导入的处理上限。与「词库容量」是两件事：这个限制的是**一次请求**的量，
# 超出部分按部分成功返回（truncated），不是拒绝——老版 500 条一次的行为。
IMPORT_LIMIT = 500

# 纯文本切分口径（老版 transforms / service_title_words 的粘贴格式）：
# 换行认 \r\n / \r / \n（和 splitlines 对这三种的处理一致），其余按老版那组
# 分隔符切；`#` 起头的整行是注释。
LINE_SPLIT = re.compile(r"\r\n|\r|\n")
WORD_SPLIT = re.compile(r"[,，、;；\t]")

# 批次运行标志。新版没有老版那种「本机进程内的批次对象」，定时发布是后台线程
# 跑 run_due、图片任务是队列里的行，没有单一可靠的内存状态可查，所以这里留一个
# 显式开关：谁在跑批次谁置位。接口先存在，后续接线不用再改调用方。
#
# 用**引用计数**而不是布尔值：run_due 会把多个账号丢进 ThreadPoolExecutor 并行跑，
# 布尔开关下「第一个结束」就会把闸放开，剩下的批次立刻变成可改词库的状态；
# 而且后开始的那个批次结束时又会写 False，语义彻底错乱。计数则只在归零时才算空闲。
_building_count = 0
# 最近一次置位的单调时钟。time.monotonic() 不受系统时间被改的影响（用户改时区/
# 校时很常见），用它算「这份计数挂了多久」才是稳的。
_building_since = 0.0
_building_lock = threading.Lock()

# 计数卡住的兜底时长。批次自己是有上限的（schedule_service.RUN_DUE_TIMEOUT_SECONDS
# = 1800 秒），所以超过这个量级还没归零，只可能是线程被强杀/进程被杀在 finally
# 之前——那时候再守着计数就等于把词库永久锁死。取 30 分钟对齐那个上限。
BUILDING_STALE_SECONDS = 30 * 60

SEED_WORDS = (
    "免邮", "包邮", "全网最低价", "最低价", "厂家直销", "工厂直销", "源头工厂",
    "一件代发", "招商加盟", "招收代理", "来图定制", "来样定制", "限时特价",
    "秒杀", "清仓", "正品保证", "假一赔十", "刷单", "好评返现", "wholesale",
    "drop shipping", "free shipping", "best price", "100% original",
)


def normalize(word: Any) -> str:
    return clean_text(word, 64).casefold()


def mark_building(value: bool) -> None:
    """标记「有批次正在跑」。True 加一、False 减一，归零才算空闲。

    签名保持布尔形状：调用方只关心「我这轮开始/结束了」，加解引用计数是实现细节。
    False 减到 0 就停住（max(0, ...)）——多减一次不该把别人的批次算成结束。
    """
    global _building_count, _building_since
    with _building_lock:
        if value:
            # 只在 0 → 1 时打时间戳：并行的多个批次共享同一个「最早开始时刻」，
            # 兜底判定看的才是整批的年龄，而不是最后一个进来的那个线程。
            if _building_count <= 0:
                _building_since = time.monotonic()
            _building_count += 1
        else:
            _building_count = max(0, _building_count - 1)
            if _building_count == 0:
                _building_since = 0.0


def is_building() -> bool:
    """当前是否真的锁着（顺带处理卡死的计数）。"""
    global _building_count, _building_since
    with _building_lock:
        if _building_count <= 0:
            return False
        if _building_since and (time.monotonic() - _building_since) > BUILDING_STALE_SECONDS:
            # 读到超龄计数就地清零：只 warning 不修状态的话，require_idle 会一直拦，
            # 用户唯一的出路是重启进程——比误放行更糟。
            logger.warning("批次运行标志超龄 %ss 未归零，按空闲处理（进程可能被强杀）",
                           BUILDING_STALE_SECONDS)
            _building_count = 0
            _building_since = 0.0
            return False
        return True


def reset_building() -> None:
    """无条件清零。给测试和「用户手动解锁」用，正常路径不该调用。"""
    global _building_count, _building_since
    with _building_lock:
        _building_count = 0
        _building_since = 0.0


def require_idle() -> None:
    """批次运行期间拒绝改词库。

    为什么拒绝而不是排队：导入/删除会把词库整批换掉，正在跑的批次前半批和
    后半批用的就是两套规则，清理结果无法复现，也没法向用户解释。
    """
    if is_building():
        raise errors.busy("批次运行中，暂不能修改词库，请等本轮结束")


def _validate_word(word: Any) -> tuple[str, str]:
    text = clean_text(word, 64)
    if not text:
        raise errors.validation("词不能为空", field="word")
    if len(text) > 64:
        raise errors.validation("词不能超过 64 个字符", field="word", value=len(text))
    if CONTROL.search(str(word or "")):
        raise errors.validation("词不能包含控制字符", field="word")
    return text, text.casefold()


def _counts(db: Session) -> dict[str, Any]:
    remove = db.query(ViolationWord).count()
    return {"remove": remove, "review": 0, "limit": WORD_LIMIT,
            "remaining": max(0, WORD_LIMIT - remove)}


def serialize(row: ViolationWord) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "word": row.word,
        "normalized": row.normalized,
        "action": "remove",
        "source": row.source,
        "enabled": bool(row.enabled),
        "createdAt": row.created_at,
    }


def list_words(db: Session, *, action: str | None = None, q: str | None = None,
               page: int = 1, page_size: int = 200) -> dict[str, Any]:
    if action is not None and action not in ACTIONS:
        raise errors.validation("action 只能是 remove", field="action", value=action)
    if page < 1:
        raise errors.validation("页码必须不小于 1", field="page", value=page)
    if not 1 <= page_size <= 500:
        raise errors.validation("每页数量必须在 1 到 500 之间", field="pageSize", value=page_size)
    query = db.query(ViolationWord)
    if action == "remove":
        query = query.filter(ViolationWord.action.in_(("remove", "review")))
    if q:
        query = query.filter(ViolationWord.word.ilike(f"%{str(q).strip()}%"))
    total = query.count()
    rows = (query.order_by(ViolationWord.id.asc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "items": [serialize(row) for row in rows],
        "page": page,
        "pageSize": page_size,
        "total": total,
        "hasMore": page * page_size < total,
        "counts": _counts(db),
    }


def export_words(db: Session) -> dict[str, Any]:
    """Return a portable, lossless snapshot of the current word rows."""
    rows = db.query(ViolationWord).order_by(ViolationWord.id.asc()).all()
    return {
        "format": "aohuang.violation-words",
        "version": 1,
        "items": [
            {
                "word": row.word,
                "action": row.action,
                "source": row.source,
                "enabled": bool(row.enabled),
                "createdAt": row.created_at,
            }
            for row in rows
        ],
    }


def _exists(db: Session, normalized: str) -> ViolationWord | None:
    return db.query(ViolationWord).filter(ViolationWord.normalized == normalized).one_or_none()


def _check_limit(db: Session, adding: int, action: str) -> None:
    if action != "remove":
        return
    current = db.query(ViolationWord).filter(ViolationWord.action == "remove").count()
    if current + adding > WORD_LIMIT:
        raise errors.AppError(
            errors.WORD_LIMIT_EXCEEDED,
            f"删除类词条已达上限 {WORD_LIMIT} 条（当前 {current} 条，本次要加 {adding} 条）",
            status=422,
            details={"limit": WORD_LIMIT, "current": current, "adding": adding},
        )


def add(db: Session, word: Any, *, action: str = "remove", source: str = "manual"
        ) -> dict[str, Any]:
    if action not in ACTIONS:
        raise errors.validation("category 只能是 remove", field="category", value=action)
    text, normalized = _validate_word(word)
    existing = _exists(db, normalized)
    if existing is not None:
        if existing.action != action:
            raise errors.AppError(
                errors.WORD_CONFLICT,
                f"「{text}」已存在于「{'删除' if existing.action == 'remove' else '复核'}」分类",
                status=409,
                details={"existing": existing.action, "word": text},
            )
        raise errors.AppError(errors.DUPLICATE_WORD, f"「{text}」已在词库里",
                              status=409, details={"existing": existing.action})
    _check_limit(db, 1, action)
    row = ViolationWord(word=text, normalized=normalized, action=action,
                        source=source, enabled=True, created_at=now_ts())
    db.add(row)
    db.flush()
    return {"word": serialize(row), "counts": _counts(db)}


def remove(db: Session, word: Any) -> dict[str, Any]:
    text, normalized = _validate_word(word)
    row = _exists(db, normalized)
    if row is None:
        raise errors.not_found(f"词库里没有「{text}」", word=text)
    payload = serialize(row)
    db.delete(row)
    db.flush()
    return {"removed": payload, "counts": _counts(db)}


def remove_many(db: Session, words: Iterable[Any]) -> dict[str, Any]:
    removed = 0
    missing: list[str] = []
    for word in words:
        try:
            text, normalized = _validate_word(word)
        except errors.AppError:
            # 批量删除里混进空串/超长词时只跳过这一条：整单抛错会让用户
            # 在几百条里自己和找是哪一条不合法。
            continue
        row = _exists(db, normalized)
        if row is None:
            missing.append(text)
            continue
        db.delete(row)
        removed += 1
    db.flush()
    return {"removed": removed, "missing": missing, "counts": _counts(db)}


def split_text(value: Any) -> list[str]:
    """把纯文本词表切成候选词（老版粘贴口径）。

    注释按**整行**判定再切分隔符：`包邮; 免邮  # 注释` 里 `#` 之后整段丢弃，
    而不是只丢到下一个分号——用户写注释时不会考虑分隔符。
    """
    words: list[str] = []
    for line in LINE_SPLIT.split(str(value or "")):
        code = line.split("#", 1)[0]
        for piece in WORD_SPLIT.split(code):
            piece = piece.strip()
            if piece:
                words.append(piece)
    return words


def normalize_input(words: Any) -> tuple[list[Any], bool, list[str]]:
    """把请求里的 words 归一成候选列表。

    返回 (候选, 是否纯文本, 超长丢弃的原词)。纯文本按切分规则展开，数组直接用；
    超长词在这里就摘出来，调用方并进 skipped——不打断整单，也不静默丢弃。
    """
    if isinstance(words, str):
        candidates: list[Any] = split_text(words)
        textual = True
    elif isinstance(words, (list, tuple)):
        candidates = list(words)
        textual = False
    else:
        raise errors.validation("words 必须是数组或纯文本", field="words")
    usable: list[Any] = []
    dropped: list[str] = []
    for word in candidates:
        # 这里量的是**原词**长度：_validate_word 内部按 64 截断，
        # 所以超长词进去会被切成 64 字符存进库，用户看见的是半截词。
        text = word if isinstance(word, str) else ""
        if not text.strip():
            dropped.append(str(word or ""))
            continue
        if len(text.strip()) > 64:
            dropped.append(text.strip())
            continue
        usable.append(word)
    return usable, textual, dropped


def import_words(db: Session, words: Any, *, action: str = "remove",
                 mode: str = "append") -> dict[str, Any]:
    """批量导入。返回 added / skipped（被跳过的词本身）/ truncated / processed。

    skipped 给的是**词**而不是计数：用户要从几百条里知道哪条没进去。
    超出 IMPORT_LIMIT 的部分不报错，截断并在 truncated 里说明，剩下的再提交一次。
    """
    if action not in ACTIONS:
        raise errors.validation("category 只能是 remove", field="category", value=action)
    if mode not in ("append", "replace"):
        raise errors.validation("mode 只能是 append / replace", field="mode", value=mode)
    require_idle()

    if isinstance(words, dict):
        items = words.get("items")
        if (words.get("format") != "aohuang.violation-words"
                or words.get("version") != 1 or not isinstance(items, list)):
            raise errors.validation("JSON 词库备份格式无效或版本不受支持", field="words")
        if len(items) > WORD_LIMIT:
            raise errors.validation("JSON 词库备份超过词库容量上限", field="words")

        restored: list[tuple[str, str, str, bool, float]] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise errors.validation("JSON 备份词条格式无效", field=f"words.items.{index}")
            text, normalized = _validate_word(item.get("word"))
            if normalized in seen:
                raise errors.validation("JSON 备份包含重复词条", field=f"words.items.{index}.word")
            seen.add(normalized)
            item_action = item.get("action")
            if item_action not in ("remove", "review"):
                raise errors.validation(
                    "JSON 备份词条 action 无效",
                    field=f"words.items.{index}.action",
                )
            source = item.get("source")
            if not isinstance(source, str) or len(source) > 16:
                raise errors.validation(
                    "JSON 备份词条 source 无效",
                    field=f"words.items.{index}.source",
                )
            enabled = item.get("enabled")
            if not isinstance(enabled, bool):
                raise errors.validation("JSON 备份词条 enabled 必须是布尔值",
                                        field=f"words.items.{index}.enabled")
            created_at = item.get("createdAt")
            if (isinstance(created_at, bool) or not isinstance(created_at, (int, float))
                    or not math.isfinite(created_at)):
                raise errors.validation("JSON 备份词条 createdAt 无效",
                                        field=f"words.items.{index}.createdAt")
            # Legacy review entries now follow the product's remove-only behavior.
            restored.append((text, normalized, source, enabled, float(created_at)))

        if mode == "replace":
            db.query(ViolationWord).delete(synchronize_session=False)
            db.flush()
        existing = {row.normalized for row in db.query(ViolationWord).all()}
        fresh = [item for item in restored if item[1] not in existing]
        skipped = [item[0] for item in restored if item[1] in existing]
        if len(existing) + len(fresh) > WORD_LIMIT:
            raise errors.AppError(
                errors.WORD_LIMIT_EXCEEDED,
                f"删除类词条已达上限 {WORD_LIMIT} 条",
                status=422,
                details={"limit": WORD_LIMIT, "current": len(existing),
                         "adding": len(fresh)},
            )
        for text, normalized, source, enabled, created_at in fresh:
            db.add(ViolationWord(
                word=text, normalized=normalized,
                action="remove", source=source, enabled=enabled,
                created_at=created_at,
            ))
        db.flush()
        return {
            "added": len(fresh), "skipped": skipped,
            "skippedCount": len(skipped), "truncated": False,
            "processed": len(restored), "limit": WORD_LIMIT,
            "textual": False, "counts": _counts(db),
        }

    usable, textual, dropped = normalize_input(words)
    skipped: list[str] = list(dropped)
    truncated = False
    processed = len(usable)
    if processed > IMPORT_LIMIT:
        # 纯文本按切分后的词数算，数组按元素个数算，两者口径一致：
        # 都是「这次真正会处理的词数」，不是原始行数。
        truncated = True
        processed = IMPORT_LIMIT
        skipped.extend(str(word) for word in usable[IMPORT_LIMIT:])
        usable = usable[:IMPORT_LIMIT]
        if not usable:
            raise errors.validation("没有可导入的有效词", field="words")

    cleaned: list[tuple[str, str]] = []
    for word in usable:
        try:
            cleaned.append(_validate_word(word))
        except errors.AppError:
            if isinstance(word, str) and word.strip():
                skipped.append(word.strip())
            continue

    unique: dict[str, str] = {}
    for text, normalized in cleaned:
        if normalized in unique:
            # 批内重复：同一个词出现两次不是错误，第二次没有可加的，算跳过。
            skipped.append(text)
            continue
        unique[normalized] = text
    if not unique:
        raise errors.validation("没有可导入的有效词", field="words")
    if mode == "replace":
        db.query(ViolationWord).delete(synchronize_session=False)
        db.flush()
    existing = {row.normalized for row in db.query(ViolationWord).all()}
    fresh: dict[str, str] = {}
    for normalized, text in unique.items():
        if normalized in existing:
            skipped.append(text)
            continue
        fresh[normalized] = text
    if action == "remove":
        current = db.query(ViolationWord).count()
        remaining = max(0, WORD_LIMIT - current)
        if len(fresh) > remaining:
            # 词库容量满时同样按部分成功处理（老版行为）：能放多少放多少，
            # 放不下的回显到 skipped。整单 422 会让用户已经粘好的词表白导一次，
            # 而他改完清单一提交又撞同一个上限。
            truncated = True
            overflow = list(fresh.items())[remaining:]
            for _normalized, text in overflow:
                skipped.append(text)
            fresh = dict(list(fresh.items())[:remaining])
            if not fresh:
                raise errors.AppError(
                    errors.WORD_LIMIT_EXCEEDED,
                    f"删除类词条已达上限 {WORD_LIMIT} 条，本次没有可导入的词",
                    status=422,
                    details={"limit": WORD_LIMIT, "current": current,
                             "skipped": len(skipped)},
                )
    for normalized, text in fresh.items():
        db.add(ViolationWord(word=text, normalized=normalized, action=action,
                             source="import", enabled=True, created_at=now_ts()))
    db.flush()
    return {
        "added": len(fresh),
        "skipped": skipped,
        "skippedCount": len(skipped),
        "truncated": truncated,
        "processed": processed,
        "limit": IMPORT_LIMIT,
        "textual": textual,
        "counts": _counts(db),
    }


def clear(db: Session, *, action: str = "remove", confirm: bool = False) -> dict[str, Any]:
    if confirm is not True:
        raise errors.AppError(errors.CONFIRM_REQUIRED,
                              "清空词库需要 confirm: true", status=400)
    require_idle()
    removed = db.query(ViolationWord).delete(synchronize_session=False)
    db.flush()
    return {"removed": int(removed or 0), "counts": _counts(db)}


def set_enabled(db: Session, word: Any = None, *, enabled: bool,
                words: Iterable[Any] | None = None) -> dict[str, Any]:
    """启用/停用词条（不删除），与老版语义一致（审查 P2-10）。"""
    require_idle()
    targets: list[str] = []
    if words:
        for item in words:
            try:
                _text, normalized = _validate_word(item)
            except errors.AppError:
                continue
            targets.append(normalized)
    elif word not in (None, ""):
        try:
            _text, normalized = _validate_word(word)
        except errors.AppError:
            raise
        targets.append(normalized)
    if not targets:
        raise errors.validation("请提供要启停的词", field="word")
    rows = (db.query(ViolationWord)
            .filter(ViolationWord.normalized.in_(targets)).all())
    for row in rows:
        row.enabled = bool(enabled)
    db.flush()
    missing = sorted(set(targets) - {row.normalized for row in rows})
    return {
        "updated": len(rows),
        "missing": missing,
        "enabled": bool(enabled),
        "counts": _counts(db),
    }


def apply_action(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /violation-words 的统一入口（action 语义沿用旧实现）。"""
    action = str(payload.get("action") or "").strip()
    category = str(payload.get("category") or "remove").strip() or "remove"
    if not action:
        raise errors.validation("action 不能为空", field="action")
    if action == "add":
        require_idle()
        result = add(db, payload.get("word"), action=category)
        return {"items": [result["word"]], "added": 1, "removed": 0, "skipped": [],
                "counts": result["counts"]}
    if action == "remove":
        require_idle()
        result = remove(db, payload.get("word"))
        return {"items": [], "removedItem": result["removed"], "added": 0,
                "removed": 1, "skipped": [], "counts": result["counts"]}
    if action == "import":
        # 这里有话要在拿到结果前说：words 既可以是数组也可以是纯文本，
        # 所以不在这里做类型/长度校验，全部交给 import_words（超限是截断不是 400）。
        raw = payload.get("words")
        if raw in (None, "", []):
            raise errors.validation("words 必须是非空数组或纯文本", field="words")
        result = import_words(db, raw, action=category,
                              mode=str(payload.get("mode") or "append"))
        return {"items": [], "added": result["added"], "removed": 0,
                "skipped": result["skipped"], "skippedCount": result["skippedCount"],
                "truncated": result["truncated"], "processed": result["processed"],
                "counts": result["counts"]}
    if action == "remove-many":
        require_idle()
        words = payload.get("words")
        if isinstance(words, str):
            words = split_text(words)
        if not isinstance(words, list) or not words:
            raise errors.validation("words 必须是非空数组或纯文本", field="words")
        result = remove_many(db, words)
        return {"items": [], "added": 0, "removed": result["removed"],
                "skipped": list(result["missing"]), "missing": result["missing"],
                "counts": result["counts"]}
    if action in ("enable", "disable"):
        result = set_enabled(db, payload.get("word"),
                             enabled=(action == "enable"),
                             words=payload.get("words"))
        return {"items": [], "added": 0, "removed": 0, "skipped": list(result["missing"]),
                "updated": result["updated"], "enabled": result["enabled"],
                "missing": result["missing"], "counts": result["counts"]}
    if action == "clear":
        result = clear(db, action=category, confirm=payload.get("confirm") is True)
        return {"items": [], "added": 0, "removed": result["removed"], "skipped": [],
                "counts": result["counts"]}
    raise errors.validation(
        "action 只能是 add / remove / import / remove-many / clear / enable / disable",
        field="action", value=action)


def seed(db: Session) -> int:
    """首次启动写入内置词（source=seed），已存在的跳过。"""
    db.query(ViolationWord).filter(ViolationWord.action == "review").update(
        {ViolationWord.action: "remove"}, synchronize_session=False
    )
    existing = {row.normalized for row in db.query(ViolationWord).all()}
    added = 0
    for word in SEED_WORDS:
        text, normalized = _validate_word(word)
        if normalized in existing:
            continue
        db.add(ViolationWord(word=text, normalized=normalized, action="remove",
                             source="seed", enabled=True, created_at=now_ts()))
        existing.add(normalized)
        added += 1
    db.flush()
    return added


def validate_library(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """返回同时出现在 remove 与 review 里的词（冲突列表，按词排序）。

    库里 normalized 唯一，理论上不该出现交叉；但 replace 导入 + 手工改写、
    或旧库迁移都可能留下。reading 侧兜住：冲突时按 remove 处理（更严格），
    至少不会因为该删的词被当成「仅复核」而发出去。
    """
    remove: set[str] = set()
    review: set[str] = set()
    for word, action in pairs:
        key = str(word or "").strip().casefold()
        if not key:
            continue
        if action == "remove":
            remove.add(key)
        elif action == "review":
            review.add(key)
    return sorted(remove & review)


def matcher(db: Session) -> list[tuple[str, str]]:
    """(normalized, action) 列表，供标题清理使用。

    同一个词两种动作都在时只保留 remove：clean_title 里 review 只记录不删，
    保留两条会让「该删」变成「只记录」，等于放行。
    """
    rows = (db.query(ViolationWord)
            .filter(ViolationWord.enabled.is_(True)).all())
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        normalized = row.normalized
        key = str(normalized or "").strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append((normalized, "remove"))
    return result


def library(db: Session) -> dict[str, Any]:
    """按动作分组的词库：{"remove": [...], "review": [...], "conflicts": [...]}。

    批量变换要用「原词」而不是 normalized（那是压过大小写/空格的匹配键），
    所以这里从原词列取。conflicts 是读取侧的交叉校验结果，非空时说明库被
    绕过接口改过；matcher() 已按 remove 优先兜底，这里只做暴露。
    """
    rows = (db.query(ViolationWord)
            .filter(ViolationWord.enabled.is_(True)).all())
    result: dict[str, Any] = {"remove": [], "review": [], "conflicts": []}
    for row in rows:
        word = str(row.word or row.normalized or "").strip()
        if word:
            result["remove"].append(word)
    return result


def _word_pattern(word: str) -> re.Pattern[str]:
    """把词编成正则（老版 transforms._title_word_pattern 的口径）。

    - 词内空格/连字符允许任意组合匹配：「free shipping」也能命中「free-shipping」
    - 英文/数字开头结尾的词加词边界，避免删 `free` 把 `freedom` 砍掉
    - 中文词不加边界（中文没有空格分词，加了反而匹配不到）
    """
    # Ignore separators already present in the entry and allow them between every
    # character, so both "free shipping" and obfuscated "f-r-e-e s-h-i-p-p-i-n-g" match.
    normalized_word = unicodedata.normalize("NFKC", str(word or "")).casefold()
    characters = [char for char in normalized_word
                  if char not in " \t\r\n-‐‑‒–—"]
    # Invisible formatting separators can be inserted to evade literal checks.
    separator = r"[\s\-‐‑‒–—\u00ad\u200b-\u200f\u2060\ufeff]*"
    pattern = separator.join(re.escape(char) for char in characters) or re.escape(normalized_word)
    if normalized_word and normalized_word[0].isascii() and normalized_word[0].isalnum():
        pattern = r"(?<![A-Za-z0-9_])" + pattern
    if normalized_word and normalized_word[-1].isascii() and normalized_word[-1].isalnum():
        pattern += r"(?![A-Za-z0-9_])"
    return re.compile(pattern, flags=re.IGNORECASE)


def _nfkc_view(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Return the NFKC matching view and source span for each output character."""
    current = ""
    source_spans: list[tuple[int, int]] = []
    prefix = ""
    for index, char in enumerate(text):
        prefix += char
        normalized = unicodedata.normalize("NFKC", prefix)
        common_prefix = 0
        while (common_prefix < min(len(current), len(normalized))
               and current[common_prefix] == normalized[common_prefix]):
            common_prefix += 1
        common_suffix = 0
        max_suffix = min(len(current) - common_prefix, len(normalized) - common_prefix)
        while (common_suffix < max_suffix
               and current[len(current) - common_suffix - 1]
               == normalized[len(normalized) - common_suffix - 1]):
            common_suffix += 1

        old_end = len(current) - common_suffix
        new_end = len(normalized) - common_suffix
        changed_spans = source_spans[common_prefix:old_end]
        start = min((span[0] for span in changed_spans), default=index)
        end = max((span[1] for span in changed_spans), default=index + 1)
        end = max(end, index + 1)
        source_spans = (
            source_spans[:common_prefix]
            + [(start, end)] * (new_end - common_prefix)
            + (source_spans[len(current) - common_suffix:] if common_suffix else [])
        )
        current = normalized
    return current, source_spans


def clean_title(title: str, words: Iterable[tuple[str, str]]) -> tuple[str, list[str]]:
    """按词库清理标题：remove 直接删，review 只记录不改。"""
    text = str(title or "")
    removed: list[str] = []
    match_text, source_indexes = _nfkc_view(text)
    # 长词优先：词库同时有「包邮」和「包邮费」时，先删短的会留下孤立的「费」。
    targets = [(n, a) for n, a in words if n]
    targets.sort(key=lambda pair: (-len(pair[0]), pair[0]))
    for normalized, _action in targets:
        pattern = _word_pattern(normalized)
        matches = list(pattern.finditer(match_text))
        if not matches:
            continue
        # 替换成空格而不是空串：中文词删掉后两侧会粘在一起（「跑步鞋包邮包」→「跑步鞋包」）
        covered: set[int] = set()
        for match in matches:
            start, end = match.span()
            if start < end:
                match_spans = source_indexes[start:end]
                source_start = min(span[0] for span in match_spans)
                source_end = max(span[1] for span in match_spans)
                covered.update(range(source_start, source_end))
        text = "".join(" " if index in covered else char
                       for index, char in enumerate(text))
        match_text, source_indexes = _nfkc_view(text)
        removed.append(normalized)
    # 删词后常留下空括号/空分隔符（【包邮】→【】），一并清掉
    text = re.sub(r"[【\[（(]\s*[】\]）)]", " ", text)
    # 连写分隔符折成一个：「a,,b」→「a, b」
    text = re.sub(r"([,，;；:：|/-])(?:\s*[,，;；:：|/-])+",
                  lambda m: m.group(1) + " ", text)
    text = re.sub(r"^[\s\-–—·|/\,，、]+|[\s\-–—·|/\,，、]+$", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(), removed


def matched_words(text: str, words: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return title-word hits using the same separator-tolerant rule as cleaning."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    hits: list[tuple[str, str]] = []
    for normalized, _action in words:
        if normalized and _word_pattern(normalized).search(value):
            hits.append((normalized, "remove"))
    return hits
