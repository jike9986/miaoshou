"""审计写入：谁、什么时候、对什么、做了什么、结果如何。

── 唯一一条铁律：**这里失败绝不能让业务失败** ──────────────────────
审计是「事后追溯」的辅助设施，业务写入（入仓、发布、改价）才是用户真正
在等的事。如果磁盘满 / SQLite 写锁超时让 audit_logs 写不进去，就顺手把
操作者的入仓也搞失败，那是本末倒置——用户会看到「保存失败」，而真正的原因
是一行没人看的日志。

所以这里的每个入口都整体包在 try/except 里，异常只进日志，不往上抛。
更具体地说，**用 SAVEPOINT 隔离**（`db.begin_nested()`）。为什么不是简单的
try/except：
  · 直接 db.add + db.flush 失败后，SQLAlchemy 会把会话标记成「需要回滚」，
    调用方下一次 commit 就会连带把业务写入一起丢掉——异常是吞掉了，
    数据也一起没了，比抛异常更糟；
  · begin_nested 里失败只回滚那一段，外层会话仍然可提交。
    （已用 server/_meas 里的实验确认过：业务行照常 commit，审计行数为 0。）

为什么不写成全局中间件：中间件只看得到「POST /xxx 返回 500」，
看不到「这次保存的是哪个商品、属于哪个店铺、是第几次重试」。
那样的日志对「谁做的能查到」毫无用处。

── 两种写入通道，别混用 ────────────────────────────────────────────
  · `record()`       —— 业务成功后调用，写进**调用方的会话**，由路由自己
    commit。好处是审计行和业务行同一个事务：要么都在，要么都不在，
    不会出现「商品存进去了但没人知道是谁存的」。
  · `record_failure()` —— 业务失败后调用，用**独立的会话**立刻提交。
    必须独立：失败时调用方会话里可能躺着半截业务写入，若借用它提交，
    等于把一次失败的入仓落进库（比丢审计严重得多）。
  · `guard()`        —— 上面两条的语法糖，包一段路由逻辑，
    抛异常时自动记一条 result="failed" 再原样抛出。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from core.utils import now_ts
from models.audit_log import AuditLog
from models.operator import Operator

logger = logging.getLogger(__name__)

# message 字段是 String(500)，超长直接截断——长 SQL 错误文本会把它撑爆
MESSAGE_MAX_LENGTH = 500


def record(
    db: Session,
    *,
    operator: Operator | None,
    action: str,
    target_kind: str = "",
    target_id: str = "",
    account_id: int | None = None,
    shop_id: str = "",
    result: str = "success",
    message: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """写一条审计，进调用方的事务（由路由 commit）。**任何情况下都不抛异常。**

    operator=None 是合法调用：定时任务、内部重试这些没有 HTTP 上下文的
    路径本来就没有「谁」，记成空名字比记成一个假名字诚实。
    """
    try:
        _write(db, operator=operator, action=action, target_kind=target_kind,
               target_id=target_id, account_id=account_id, shop_id=shop_id,
               result=result, message=message, detail=detail)
    except Exception:  # noqa: BLE001 - 见模块 docstring：审计失败不连累业务
        logger.exception("审计写入失败（已忽略，业务不受影响）action=%s target=%s/%s",
                         action, target_kind, target_id)


def _snapshot_operator(operator: Operator | None) -> Operator | None:
    """把操作员的 id/name 读成游离值，脱离会话状态。

    返回一个**不挂在任何会话上**的 `Operator` 实例，只填 id/name 两个字段。
    这样调用方回滚会话、甚至整行被删掉，`_write` 读 `getattr(operator, "id")`
    都不会触发重新加载（transient 对象没有 expired 状态）。
    """
    if operator is None:
        return None
    try:
        ident = operator.id
        name = operator.name
    except Exception:  # noqa: BLE001 - 会话已废时读属性会抛，退化成「不知道是谁」
        logger.debug("审计：操作员信息读取失败（已忽略）", exc_info=True)
        return None
    if ident is None:
        return None
    return Operator(id=ident, name=name or "", token="", enabled=True,
                    is_admin=False)


def _reattach_operator(db: Session, who: Operator | None) -> Operator | None:
    """在审计自己的会话里确认这条操作员还在；不在就只保留名字。

    `audit_logs.operator_id` 有外键（`PRAGMA foreign_keys=ON` 是开着的），
    指着一条不存在的 operator 行会直接被 SQLite 拒掉整条审计。

    什么时候会不存在：全新安装的第一次请求。`current_operator` 通过
    `ensure_default` flush 出「本机」操作员，但没 commit；业务失败后
    `record_failure` 回滚调用方会话，那行就消失了。这时候名字仍然是真信息
    （「本机」确实点了这一下），只有 id 没了 —— 按 `_write` 的两列设计，
    正是该留 name、舍 id 的场合。
    """
    if who is None:
        return None
    exists = (db.query(Operator.id).filter(Operator.id == who.id).one_or_none())
    if exists is not None:
        return who
    logger.info("审计：操作员 %s(id=%s) 已不在库中，本条只记名字不记 id",
                who.name, who.id)
    return Operator(id=None, name=who.name or "", token="", enabled=True,
                    is_admin=False)


def record_failure(
    *,
    operator: Operator | None,
    action: str,
    target_kind: str = "",
    target_id: str = "",
    account_id: int | None = None,
    shop_id: str = "",
    message: str = "",
    detail: dict[str, Any] | None = None,
    db: Session | None = None,
) -> None:
    """记一条失败。用独立会话立刻提交，不碰调用方那个可能已经废掉的会话。

    为什么必须独立会话：失败时调用方会话里可能已有半截业务写入，借它提交
    会把一次失败的入仓落进库。而且 SQL 层错误之后会话本身可能已经不可用，
    begin_nested 都开不出来。

    **为什么还要 `db` 参数并先回滚它**：SQLite（WAL 也一样）同一时刻只允许
    一个写事务。失败时调用方会话往往正握着一笔业务写入没提交——此时另开一个
    会话去 INSERT，等的就是自己那把锁，15 秒后拿到
    `OperationalError: database is locked`，**这条失败审计直接丢掉**。
    实测踩到过（confirm-save 的 hash 不匹配路径，整条请求卡 16 秒然后审计丢失）。
    所以有 `db` 就先回滚它，把写锁放掉再写审计。回滚本身是安全的：走到这里
    说明这次请求已经注定失败，那半截业务写入本来就不该提交。

    **为什么还要先快照操作员**：回滚会把调用方会话里的 ORM 对象全部过期。
    全新安装的第一次请求正是这种情形——`current_operator` 刚通过
    `ensure_default` flush 出一条「本机」操作员（还没 commit），紧接着业务
    失败、这里一回滚，那条 operator 行就没了；后面 `_write` 读
    `operator.id` 时 SQLAlchemy 去重新加载，抛 `ObjectDeletedError`，
    **这条失败审计直接丢掉**。而「第一条失败」恰恰是最需要留痕的那条。
    所以 id/name 必须在回滚之前读成普通值，且 id 指向的行可能已经随回滚消失
    ——那时只记名字，见 `_reattach_operator`。
    """
    who = _snapshot_operator(operator)
    if db is not None:
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - 会话可能已经废了，回滚失败不影响写审计
            logger.exception("审计（失败记录）前回滚调用方会话失败（已忽略）")
    try:
        from db.session import session_scope

        with session_scope() as own:
            _write(own, operator=_reattach_operator(own, who),
                   action=action, target_kind=target_kind,
                   target_id=target_id, account_id=account_id, shop_id=shop_id,
                   result="failed", message=message, detail=detail)
    except Exception:  # noqa: BLE001 - 同上：审计失败绝不连累业务
        logger.exception("审计（失败记录）写入失败（已忽略）action=%s target=%s/%s",
                         action, target_kind, target_id)


@contextmanager
def guard(
    db: Session,
    *,
    operator: Operator | None,
    action: str,
    target_kind: str = "",
    target_id: str = "",
    account_id: int | None = None,
    shop_id: str = "",
    detail: dict[str, Any] | None = None,
) -> Iterator[None]:
    """包住一段会写库的路由逻辑：成功不记（路由自己记 ok），失败记 failed 再抛。

    为什么不在成功路径上也由这里记：成功时 target_id 往往是业务函数算出来的
    （新商品的 warehouse_key、新批次 id），只有调用方知道。这里只管失败——
    失败时那个 id 恰好就是入参，不需要业务返回什么。
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - 原样抛出，只是顺手记一笔
        record_failure(
            db=db,
            operator=operator, action=action, target_kind=target_kind,
            target_id=target_id, account_id=account_id, shop_id=shop_id,
            message=_describe(exc), detail=detail)
        raise


def _describe(exc: BaseException) -> str:
    """把异常压成一行「错误码 + 人话」，给运营看，不是给开发者看栈。

    AppError 自带稳定错误码（PAYLOAD_CHANGED / READ_ONLY_MODE …），
    那才是排查时要 grep 的东西；裸异常的类名同理（OperationalError）。
    """
    code = str(getattr(exc, "code", "") or type(exc).__name__)
    text = str(getattr(exc, "message", "") or str(exc) or "").strip()
    return f"{code}: {text}" if text else code


def _next_created_at(db: Session) -> float:
    """给这条审计算一个**严格递增**的 created_at。

    为什么要动时间戳：`query()` 的游标是 `created_at < before`，如果两条
    审计落在同一毫秒（`now_ts()` 只有毫秒精度，一个请求里连写几条必然撞），
    翻到下一页时那条同步的兄弟行会被 `strict <` 直接跳过 —— 审计漏一条
    就是一次追溯失败，这比时间戳略有偏差严重得多。

    代价是同一毫秒内连写时时间会往后顶最多几毫秒（取 max(now, 上一行+1ms)），
    对「谁在几点几分做的」完全无感。并发写（两个会话同时算）仍可能撞，
    所以排序同时带 `id desc` 兜底，只是那种情况下的游标边界不保证精确。
    """
    from sqlalchemy import func

    latest = db.query(func.max(AuditLog.created_at)).scalar()
    stamp = now_ts()
    if latest is None:
        return stamp
    return max(stamp, round(float(latest) + 0.001, 3))


def _write(
    db: Session,
    *,
    operator: Operator | None,
    action: str,
    target_kind: str,
    target_id: str,
    account_id: int | None,
    shop_id: str,
    result: str,
    message: str,
    detail: dict[str, Any] | None,
) -> None:
    row = AuditLog(
        # operator_id 与 operator_name 都写：前者用来 join 现状，
        # 后者是快照，保证删号/改名之后历史仍然答得出「当时是谁」
        operator_id=getattr(operator, "id", None),
        operator_name=(getattr(operator, "name", "") or "")[:64],
        action=(action or "")[:32],
        target_kind=(target_kind or "")[:32],
        target_id=(target_id or "")[:64],
        account_id=_as_int(account_id),
        shop_id=(shop_id or "")[:32],
        result=(result or "success")[:16],
        message=(message or "")[:MESSAGE_MAX_LENGTH],
        detail=detail if isinstance(detail, dict) else {},
        created_at=_next_created_at(db),
    )
    # SAVEPOINT：这一段落盘失败只回滚这一段，外层业务写入仍可提交
    with db.begin_nested():
        db.add(row)
        db.flush()


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


# --- 查询 -----------------------------------------------------------------
def query(
    db: Session,
    *,
    operator_id: Any = None,
    action: str | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    result: str | None = None,
    limit: int = 50,
    before: float | None = None,
) -> dict[str, Any]:
    """审计查询：按 created_at 倒序，游标分页（before = 上一页的 nextBefore）。

    为什么用游标而不是 offset：审计表只增不减，翻到第 20 页时 offset 要
    扫过前 19 页的行；而且并发写入会让 offset 分页漏行/重行——审计漏一条
    等于这次追溯失败。游标（created_at < before）没有这两个问题。
    """
    q = db.query(AuditLog)
    ident = _as_int(operator_id)
    if ident is not None:
        q = q.filter(AuditLog.operator_id == ident)
    if action:
        q = q.filter(AuditLog.action == action)
    if target_kind:
        q = q.filter(AuditLog.target_kind == target_kind)
    if target_id:
        q = q.filter(AuditLog.target_id == target_id)
    if result:
        q = q.filter(AuditLog.result == result)
    if before:
        # 严格小于 + (created_at, id) 双键排序：同一毫秒的多行也能稳定翻页
        q = q.filter(AuditLog.created_at < float(before))
    size = max(1, min(int(limit or 50), 200))
    rows = (q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(size + 1).all())
    has_more = len(rows) > size
    rows = rows[:size]
    # total 是「当前筛选条件下的总条数」：界面上「共 N 条」要的是这个数，
    # 不是本页条数。多一次 count 查询换一个不会骗人的数字，值得。
    total = q.count()
    return {
        "items": [serialize(row) for row in rows],
        "total": total,
        # 下一页的游标：没有更多时为 None，前端据此停手
        "nextBefore": rows[-1].created_at if (rows and has_more) else None,
        "hasMore": has_more,
    }


def serialize(row: AuditLog) -> dict[str, Any]:
    detail = row.detail if isinstance(row.detail, dict) else {}
    return {
        "id": str(row.id),
        "operatorId": str(row.operator_id) if row.operator_id else None,
        # 名字用冗余快照：操作员被删/改名之后这里仍然是当时那个人
        "operatorName": row.operator_name or "",
        "action": row.action or "",
        "targetKind": row.target_kind or "",
        "targetId": row.target_id or "",
        # 给人看的一句话对象描述：前端优先显示它，没有才退回 kind + id。
        # 后端能拿到仓库键/商品标题/批次件数，前端拿不到，所以这句在这里拼。
        "targetLabel": _target_label(row, detail),
        "accountId": str(row.account_id) if row.account_id else None,
        "shopId": row.shop_id or "",
        "result": row.result or "success",
        "message": row.message or "",
        # 前端字段叫 details（复数）；库里列名是 detail。
        # 两个名字都带上：老调用方按 detail 取，界面按 details 取，都不破。
        "details": detail,
        "detail": detail,
        "createdAt": row.created_at,
    }


def _target_label(row: AuditLog, detail: dict[str, Any]) -> str:
    """把「对象」翻成一句人话。

    只做纯字符串拼装，不查库：列表一次几十行，逐行回查商品标题会把一次
    分页变成上百次查询，而审计页是被人反复刷的页面。
    """
    kind = (row.target_kind or "").strip()
    ident = (row.target_id or "").strip()
    name = str(detail.get("name") or detail.get("title") or "").strip()
    if kind == "product":
        return f"商品 {ident}" if ident else "商品"
    if kind == "batch":
        return f"批次 {ident}" if ident else "批次"
    if kind == "publish":
        return f"发布 {ident}" if ident else "发布"
    if kind == "operator":
        return f"操作员「{name}」" if name else (f"操作员 {ident}" if ident else "操作员")
    if kind == "schedule":
        return f"定时任务「{name}」" if name else (f"定时任务 {ident}" if ident else "定时任务")
    if kind == "setting":
        return f"共享设置「{name}」" if name else "共享设置"
    if kind == "template":
        return f"定价模板「{name}」" if name else "定价模板"
    if kind == "title":
        return f"标题 {ident}" if ident else "标题"
    if kind:
        return f"{kind} {ident}".strip()
    return ident
