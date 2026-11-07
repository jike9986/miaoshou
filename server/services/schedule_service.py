"""定时发布任务：CRUD + 到期判断（不跑后台线程）。

到期判定是纯函数（可单测，无副作用）：
  · 一天只跑一次 —— last_run_date 相同即不再跑；
  · 当天失败最多 3 次 —— attempt_date + attempts 计数；
  · weekly 用 1–7（周一为 1），monthly 用 1–31，range 含首尾且 ≤180 天。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.constants import (
    FREQUENCIES,
    SCHEDULE_MAX_ATTEMPTS_PER_DAY,
    SCHEDULE_MAX_RANGE_DAYS,
)
from core.utils import clean_text, now_ts
from models.account import Account
from models.operator import Operator
from models.schedule import Schedule
from services import account_service, operator_service
from services.account_service import get_account, require_shop

# 放在 import 之后：原来夹在两组 import 中间，ruff 的 E402 会把下面每个 import
# 都算成「不在文件顶部」。挪到这里对运行时毫无影响（logger 名不变）。
logger = logging.getLogger(__name__)

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 显式 null = 清空：只有这些字段有「空值」语义（days/day_of_month 是 JSON 数组，
# start_date/end_date 是字符串）。其余字段收到 null 视为「未提供」，沿用现值。
_NULL_CLEARS: dict[str, Any] = {
    "days": list,
    "weekdays": list,
    "dayOfMonth": list,
    "startDate": str,
    "endDate": str,
}


# --- 校验 -----------------------------------------------------------------
def valid_time(value: Any) -> str:
    match = _TIME.match(str(value or "").strip())
    if not match:
        raise errors.validation("时间须为 HH:MM，例如 09:30", field="time", value=value)
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def valid_days(value: Any, low: int, high: int, field: str, label: str) -> list[int]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise errors.validation(f"{label}须为数组", field=field)
    days: list[int] = []
    for item in value:
        try:
            day = int(item)
        except (TypeError, ValueError):
            raise errors.validation(f"{label}含无效值：{item!r}", field=field) from None
        if not low <= day <= high:
            raise errors.validation(f"{label}必须在 {low} 到 {high} 之间",
                                    field=field, value=day)
        days.append(day)
    return sorted(set(days))


def valid_date(value: Any, field: str, label: str) -> str:
    text = str(value or "").strip()
    if not _DATE.fullmatch(text):
        raise errors.validation(f"{label}须为 YYYY-MM-DD，例如 2026-10-21",
                                field=field, value=text)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise errors.validation(f"{label}不是有效日期：{text}", field=field) from None


def normalize(db: Session, raw: Mapping[str, Any], *, partial: bool = False,
              existing: Schedule | None = None) -> dict[str, Any]:
    """校验并归一化任务字段。partial=True 时只处理出现过的键。

    显式 null 表示「清空」：days / dayOfMonth → []，startDate / endDate → ""；
    其余字段收到 null 按「未提供」处理，沿用现值（accountId / shopId / template
    这类必填项不能清空）。
    """
    merged: dict[str, Any] = {}
    if existing is not None:
        merged = {
            "name": existing.name,
            "enabled": bool(existing.enabled),
            "accountId": str(existing.account_id) if existing.account_id else "",
            "shopId": existing.shop_id,
            "template": existing.template_name,
            "frequency": existing.frequency,
            "days": list(existing.days or []),
            "dayOfMonth": list(existing.day_of_month or []),
            "startDate": existing.start_date,
            "endDate": existing.end_date,
            "time": existing.time_of_day,
            "perRun": existing.per_run,
            "selectedProductIds": list(existing.selected_product_ids or []),
            "plannedRuns": existing.planned_runs,
            "claimOnly": bool(existing.claim_only),
        }
    provided = set(raw.keys())
    merged.update({key: value for key, value in raw.items() if value is not None})

    def pick(key: str, alias: str | None = None) -> Any:
        for name in (key, alias):
            if not name or name not in provided:
                continue
            value = raw[name]
            if value is None:
                # 显式 null：可清空的字段落成空值，其余视为未提供
                if name in _NULL_CLEARS:
                    return _NULL_CLEARS[name]()
                continue
            return value
        return merged.get(key)

    account_raw = pick("accountId", "account")
    account_id = str(account_raw or "").strip()
    if not account_id:
        raise errors.validation("请选择定时发布用哪个账号", field="accountId")
    account = get_account(db, account_id)
    if account is None:
        raise errors.not_found(f"账号 {account_id} 不存在", accountId=account_id)

    shop_id = str(pick("shopId") or "").strip()
    if not shop_id:
        raise errors.validation("请选择定时发布到哪个店铺", field="shopId")
    if not shop_id.isdigit():
        raise errors.validation("shopId 必须是数字", field="shopId", value=shop_id)

    template = str(pick("template", "templateName") or "").strip()
    if not template and not partial:
        raise errors.validation("请选择用哪套定价模板", field="template")
    # 局部更新（PATCH）没带 template 时沿用原值；原值也为空就不校验，
    # 免得「只想清空日期」被模板字段挡住（审查 P1-10）

    frequency = str(pick("frequency") or "daily").strip()
    if frequency not in FREQUENCIES:
        # 中文界面里弹英文枚举，用户不知道要填什么
        raise errors.validation("频率只能是仅一次 / 每天 / 每周 / 每月 / 日期区间",
                                field="frequency", value=frequency)

    per_run_raw = pick("perRun")
    if per_run_raw in (None, "") and not partial:
        raise errors.validation("每次发布数量不能为空", field="perRun")
    if per_run_raw in (None, ""):
        per_run = int(existing.per_run or 10) if existing is not None else 10
    else:
        try:
            per_run = int(per_run_raw)
        except (TypeError, ValueError):
            raise errors.validation("每次发布数量必须是整数", field="perRun") from None
        if not 1 <= per_run <= 2000:
            raise errors.validation("每次发布数量必须在 1 到 2000 之间",
                                    field="perRun", value=per_run)

    selected_raw = pick("selectedProductIds")
    selected_product_ids = list(dict.fromkeys(
        str(value).strip() for value in (selected_raw or []) if str(value).strip()))
    planned_raw = pick("plannedRuns")
    planned_runs = None if planned_raw in (None, "") else int(planned_raw)
    if selected_product_ids and planned_runs is None:
        raise errors.validation("选择商品后，请设置计划发布天数", field="plannedRuns")
    if selected_product_ids and len(selected_product_ids) > per_run * (planned_runs or 0):
        raise errors.validation(
            f"已选 {len(selected_product_ids)} 件，但 {planned_runs} 天最多发布 {per_run * (planned_runs or 0)} 件",
            field="selectedProductIds")

    time_value = pick("time", "timeOfDay")
    if time_value in (None, "") and existing is None and not partial:
        raise errors.validation("执行时间不能为空", field="time")
    time_text = (valid_time(time_value) if time_value not in (None, "")
                 else (existing.time_of_day if existing is not None else "09:00"))

    days = valid_days(pick("days", "weekdays"), 1, 7, "days", "星期") \
        if frequency == "weekly" else []
    day_of_month = valid_days(pick("dayOfMonth"), 1, 31, "dayOfMonth", "日期") \
        if frequency == "monthly" else []
    if frequency == "weekly" and not days:
        raise errors.validation("每周发布需要选择至少一个星期几", field="days")
    if frequency == "monthly" and not day_of_month:
        raise errors.validation("每月发布需要选择至少一个日期", field="dayOfMonth")

    start_date = end_date = ""
    if frequency == "once":
        # 仅一次：必须指定 startDate
        start_date = valid_date(pick("startDate"), "startDate", "执行日期")
    if frequency == "range":
        start_date = valid_date(pick("startDate"), "startDate", "开始日期")
        end_date = valid_date(pick("endDate"), "endDate", "结束日期")
        if end_date < start_date:
            raise errors.unprocessable("结束日期不能早于开始日期",
                                       startDate=start_date, endDate=end_date)
        span = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
        if span > SCHEDULE_MAX_RANGE_DAYS:
            raise errors.unprocessable(
                f"指定日期最多 {SCHEDULE_MAX_RANGE_DAYS} 天，当前是 {span} 天",
                startDate=start_date, endDate=end_date, span=span)

    # 时间不能早于当前时刻（负责人 2026-11-06 明确）。
    # 今天这一轮已经错过，建完立刻显示「已逾期」只会让人困惑。
    # 局部更新（PATCH）只在改了时间/日期时才校验，避免旧任务改名字被拦。
    _now = datetime.now()
    _today_iso = _now.date().isoformat()
    _now_hm = _now.strftime("%H:%M")
    _check_time = (not partial) or ("time" in raw) or ("startDate" in raw)
    if _check_time:
        if frequency == "once" and start_date and start_date < _today_iso:
            raise errors.validation("执行日期不能早于今天", field="startDate",
                                    value=start_date)
        if frequency == "range" and start_date and start_date < _today_iso:
            raise errors.validation("开始日期不能早于今天", field="startDate",
                                    value=start_date)
        if frequency == "once" and start_date == _today_iso and time_text < _now_hm:
            raise errors.validation(
                f"执行时间不能早于当前时间 {_now_hm}", field="time", value=time_text)
        if frequency in ("daily", "weekly", "monthly") and time_text < _now_hm:
            raise errors.validation(
                f"执行时间不能早于当前时间 {_now_hm}", field="time", value=time_text)

    name = clean_text(pick("name") or "", 120)
    if len(name) > 120:
        raise errors.validation("任务名不能超过 120 个字符", field="name")
    if not name:
        name = f"{account.name} 定时发布"

    enabled_raw = pick("enabled")
    enabled = bool(existing.enabled) if (enabled_raw is None and existing) else bool(
        True if enabled_raw is None else enabled_raw)

    return {
        "name": name,
        "enabled": enabled,
        "account_id": account.id,
        "shop_id": shop_id,
        "template_name": template,
        "frequency": frequency,
        "days": days,
        "day_of_month": day_of_month,
        "weekdays": days,
        "start_date": start_date,
        "end_date": end_date,
        "time_of_day": time_text,
        "per_run": per_run,
        "selected_product_ids": selected_product_ids or None,
        "planned_runs": planned_runs,
        "claim_only": bool(pick("claimOnly") if "claimOnly" in provided else
                           (existing.claim_only if existing else False)),
    }


# --- 到期判断（纯函数） ---------------------------------------------------
def is_scheduled_day(task: Mapping[str, Any], day: date) -> bool:
    frequency = task.get("frequency")
    if frequency == "once":
        # 仅一次：只在 startDate 那天跑
        return bool(str(task.get("startDate") or "")) \
            and day.isoformat() == str(task.get("startDate"))
    if frequency == "daily":
        return True
    if frequency == "weekly":
        return day.isoweekday() in (task.get("days") or [])
    if frequency == "monthly":
        return day.day in (task.get("dayOfMonth") or [])
    if frequency == "range":
        start = str(task.get("startDate") or "")
        end = str(task.get("endDate") or "")
        return bool(start and end) and start <= day.isoformat() <= end
    return False


def next_due(task: Mapping[str, Any], now: datetime) -> datetime | None:
    """下一次执行时刻；停用返回 None。最多向前看 366 天。"""
    if not task.get("enabled", True):
        return None
    raw = str(task.get("time") or "09:00")
    hour, minute = (int(part) for part in raw.split(":"))
    for offset in range(0, 366):
        day = (now + timedelta(days=offset)).date()
        if not is_scheduled_day(task, day):
            continue
        moment = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
        if moment > now:
            return moment
    return None


def is_due(task: Mapping[str, Any], now: datetime) -> bool:
    """到期 = 启用 + 今天匹配 + 到点 + 今天还没跑过。"""
    if not task.get("enabled", True):
        return False
    today = now.date().isoformat()
    if str(task.get("lastRunDate") or "") == today:
        return False
    if not is_scheduled_day(task, now.date()):
        return False
    hour, minute = (int(part) for part in str(task.get("time") or "09:00").split(":"))
    return (now.hour, now.minute) >= (hour, minute)


def attempts_today(task: Mapping[str, Any], now: datetime) -> int:
    if str(task.get("attemptDate") or "") != now.date().isoformat():
        return 0
    return int(task.get("attempts") or 0)


def should_run(task: Mapping[str, Any], now: datetime) -> bool:
    return is_due(task, now) and attempts_today(task, now) < SCHEDULE_MAX_ATTEMPTS_PER_DAY


def due_tasks(db: Session, now: datetime | None = None) -> list[Schedule]:
    moment = now or datetime.now()
    rows = db.query(Schedule).filter(Schedule.enabled.is_(True)).all()
    return [row for row in rows if should_run(as_dict(row), moment)]


# --- 序列化 ---------------------------------------------------------------
def as_dict(row: Schedule) -> dict[str, Any]:
    return {
        "name": row.name,
        "enabled": bool(row.enabled),
        "frequency": row.frequency,
        "days": list(row.days or []),
        "dayOfMonth": list(row.day_of_month or []),
        "startDate": row.start_date or "",
        "endDate": row.end_date or "",
        "time": row.time_of_day,
        "perRun": int(row.per_run or 0),
        "selectedProductIds": list(row.selected_product_ids or []),
        "plannedRuns": row.planned_runs,
        "completed": bool(row.completed),
        "lastRunDate": row.last_run_date or "",
        "attemptDate": row.attempt_date or "",
        "attempts": int(row.attempts or 0),
        "claimOnly": bool(row.claim_only),
    }


def serialize(row: Schedule, *, account_name: str = "", shop_name: str = "") -> dict[str, Any]:
    due = next_due(as_dict(row), datetime.now())
    return {
        "id": row.id,
        "name": row.name,
        "enabled": bool(row.enabled),
        "accountId": str(row.account_id) if row.account_id else None,
        "account": account_name,
        "shopId": row.shop_id,
        "shopName": shop_name,
        "template": row.template_name,
        "frequency": row.frequency,
        "days": list(row.days or []),
        "dayOfMonth": list(row.day_of_month or []),
        "startDate": row.start_date or "",
        "endDate": row.end_date or "",
        "time": row.time_of_day,
        "perRun": int(row.per_run or 0),
        "selectedProductIds": list(row.selected_product_ids or []),
        "plannedRuns": row.planned_runs,
        "completed": bool(row.completed),
        "claimOnly": bool(row.claim_only),
        "lastRunDate": row.last_run_date or "",
        "attemptDate": row.attempt_date or "",
        "attempts": int(row.attempts or 0),
        "nextDueAt": (due.timestamp() if due else None),
        "nextDueText": (due.strftime("%Y-%m-%d %H:%M") if due else "已停用"),
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
        "lastResult": row.last_result if isinstance(row.last_result, dict) else {},
        # 此刻正在执行（内存态，不落库）：界面据此显示「执行中」并自动刷新。
        "running": task_running(row.id),
    }


def _shop_name_of(db: Session, account: Account | None, shop_id: str) -> str:
    if account is None:
        return ""
    from services.account_service import cached_shops

    for shop in cached_shops(account):
        if str(shop.get("shopId")) == str(shop_id):
            return str(shop.get("name") or "")
    return ""


# --- 执行历史（老版 publish-schedule-log.json） ----------------------------
LOG_LIMIT_DEFAULT = 100
LOG_LIMIT_MAX = 2000
# 每个任务保留多少条历史。老版全局封顶 5000 行；按任务分别裁剪，
# 免得安静的任务被活跃任务把历史挤光。
LOG_RETAIN = 500


def record_log(db: Session, task: Schedule, summary: Mapping[str, Any],
               *, moment: datetime | None = None) -> None:
    """把一次执行结果追加进历史表。

    老版原话：「A year of five-a-day runs is under 2000 rows; cap defensively
    at 5000.」这里同样设上限，但按任务分别裁剪——全局裁剪会让安静的任务
    被活跃任务挤掉历史。
    """
    from models.schedule_log import ScheduleLog

    entry = ScheduleLog(
        task_id=str(task.id),
        task_name=str(task.name or ""),
        account_id=task.account_id,
        owner_operator_id=task.owner_operator_id,
        status=str(summary.get("status") or ""),
        reason=str(summary.get("reason") or "")[:500],
        requested=int(summary.get("requested") or 0),
        selected=int(summary.get("selected") or 0),
        published=int(summary.get("published") or 0),
        failed=int(summary.get("failed") or 0),
        # The history table exposes aggregate fields only. Keep the receipt
        # identities intact rather than slicing JSON in the middle of a string.
        summary=json.dumps({key: value for key, value in summary.items()
                            if key != "results"}, ensure_ascii=False, default=str),
        at=float(moment.timestamp()) if moment else now_ts(),
    )
    db.add(entry)
    db.flush()
    # 裁剪：每个任务只留最近 LOG_RETAIN 条
    rows = (db.query(ScheduleLog.id)
            .filter(ScheduleLog.task_id == str(task.id))
            .order_by(ScheduleLog.at.desc())
            .offset(LOG_RETAIN).all())
    if rows:
        db.query(ScheduleLog).filter(
            ScheduleLog.id.in_([row[0] for row in rows])).delete(synchronize_session=False)
        db.flush()


def list_logs(db: Session, *, task_id: str = "", account_id: Any = None,
              limit: Any = LOG_LIMIT_DEFAULT,
              operator: Operator | None = None) -> dict[str, Any]:
    """执行历史，新的在前。"""
    from models.schedule_log import ScheduleLog

    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = LOG_LIMIT_DEFAULT
    count = max(1, min(LOG_LIMIT_MAX, count))
    query = db.query(ScheduleLog)
    task_text = str(task_id or "").strip()
    if task_text:
        # Authorize through the task's current account before exposing its history.
        get_schedule(db, task_text, operator=operator)
        query = query.filter(ScheduleLog.task_id == task_text)
    if account_id not in (None, ""):
        text = str(account_id).strip()
        if text.isdigit():
            _assert_operator_account_access(db, int(text), operator)
            query = query.filter(ScheduleLog.account_id == int(text))
        else:
            # 无效账号筛选不应退化成“查看全部历史”。
            query = query.filter(ScheduleLog.account_id == -1)
    elif not task_text:
        visible = _visible_account_ids(db, operator)
        if visible is not None:
            query = query.filter(ScheduleLog.account_id.in_(visible or [-1]))
    if operator is not None and not _is_global_operator(operator):
        query = query.filter(ScheduleLog.owner_operator_id == operator.id)
    total = query.count()
    rows = (query.order_by(ScheduleLog.at.desc(), ScheduleLog.id.desc())
            .limit(count).all())
    return {
        "items": [{
            "id": row.id,
            "taskId": row.task_id,
            "taskName": row.task_name,
            "accountId": str(row.account_id) if row.account_id else None,
            "status": row.status,
            "reason": row.reason,
            "requested": row.requested,
            "selected": row.selected,
            "published": row.published,
            "failed": row.failed,
            "submitted": _log_summary(row.summary).get("submitted", 0),
            "at": row.at,
        } for row in rows],
        "total": total,
        "limit": count,
    }


# --- CRUD -----------------------------------------------------------------
def _is_global_operator(operator: Operator | None) -> bool:
    return (operator is None
            or operator_service.role_of(operator) in {
                operator_service.ROLE_ADMIN, operator_service.ROLE_TEACHER})


def _assert_operator_account_access(db: Session, account_id: Any,
                                    operator: Operator | None) -> None:
    if operator is None or _is_global_operator(operator):
        return
    try:
        account_pk = int(str(account_id))
    except (TypeError, ValueError):
        raise errors.not_found(f"账号 {account_id} 不存在", accountId=str(account_id)) from None
    if not account_service.has_account_access(db, account_pk, operator.id):
        raise errors.AppError(errors.FORBIDDEN, "无权访问其他学员的定时任务", status=403)


def _assert_member_schedule_mode(operator: Operator | None,
                                 claim_only: bool) -> None:
    """Members may schedule publication of claimed products, not claim jobs."""
    if (operator is not None
            and operator_service.role_of(operator) == operator_service.ROLE_MEMBER
            and claim_only):
        raise errors.forbidden("学员定时任务不能使用认领模式，请关闭 claimOnly")


def _assert_schedule_owner(row: Schedule, operator: Operator | None) -> None:
    """Keep tasks private within a shared Miaoshou account."""
    if _is_global_operator(operator):
        return
    if row.owner_operator_id != operator.id:
        raise errors.not_found(f"定时任务 {row.id} 不存在", taskId=str(row.id))


def _visible_account_ids(db: Session, operator: Operator | None) -> list[int] | None:
    if _is_global_operator(operator):
        return None
    return [row.id for row in db.query(Account).all()
            if account_service.has_account_access(db, row.id, operator.id)]


def list_schedules(db: Session, *, account_id: Any = None,
                   enabled: bool | None = None,
                   operator: Operator | None = None) -> dict[str, Any]:
    query = db.query(Schedule)
    if account_id not in (None, ""):
        text = str(account_id)
        if text.isdigit():
            _assert_operator_account_access(db, int(text), operator)
            query = query.filter(Schedule.account_id == int(text))
        else:
            # 无效账号筛选不应退化成“查看全部任务”。
            query = query.filter(Schedule.account_id == -1)
    else:
        visible = _visible_account_ids(db, operator)
        if visible is not None:
            query = query.filter(Schedule.account_id.in_(visible or [-1]))
    if operator is not None and not _is_global_operator(operator):
        query = query.filter(Schedule.owner_operator_id == operator.id)
    if enabled is not None:
        query = query.filter(Schedule.enabled.is_(bool(enabled)))
    rows = query.order_by(Schedule.created_at.asc()).all()
    accounts = {row.id: row for row in db.query(Account).all()}
    items = []
    for row in rows:
        account = accounts.get(row.account_id)
        items.append(serialize(row, account_name=account.name if account else "",
                              shop_name=_shop_name_of(db, account, row.shop_id)))
    # 带上调度运行态：扫描间隔、账号节流间隔、后台循环是否还活着。
    # 只给任务列表的话，后台线程静默死掉时界面完全看不出来。
    from services.miaoshou import UPSTREAM_PACE_SECONDS

    return {
        "items": items,
        "total": len(items),
        "serverTime": now_ts(),
        "tickSeconds": SCAN_INTERVAL_SECONDS,
        "paceSeconds": UPSTREAM_PACE_SECONDS,
        "running": _scheduler_alive(),
    }


# 后台扫描间隔，必须与 main._schedule_loop 的 60s 一致
SCAN_INTERVAL_SECONDS = 60
_scheduler_heartbeat = {"at": 0.0}

# 正在执行的任务（task_id -> 开始时间）。发布一件要 20-60 秒，期间界面上
# 「上次结果」还是上一轮的，用户看到的就是「任务没启动」。只放内存：进程重启
# 后这批执行已经断了，残留状态比没有更误导。
_running_tasks: dict[str, float] = {}
# 执行中标记的最长存活时间：线程异常退出时没人清，超时自动失效。
RUNNING_MARK_TTL_SECONDS = 3600.0


def mark_task_running(task_id: str) -> None:
    _running_tasks[str(task_id)] = now_ts()


def clear_task_running(task_id: str) -> None:
    _running_tasks.pop(str(task_id), None)


def task_running(task_id: str) -> bool:
    """这个任务此刻是否正在执行（供列表展示「执行中」）。"""
    started = _running_tasks.get(str(task_id))
    if started is None:
        return False
    if now_ts() - started > RUNNING_MARK_TTL_SECONDS:
        _running_tasks.pop(str(task_id), None)
        return False
    return True


def mark_scheduler_tick() -> None:
    """后台每扫一轮打一次心跳（main._schedule_loop 调用）。"""
    import time

    _scheduler_heartbeat["at"] = time.time()


def _scheduler_alive() -> bool:
    """两轮间隔内有过心跳就算活着；从没打过心跳（没启动）算 False。"""
    import time

    last = _scheduler_heartbeat["at"]
    if not last:
        return False
    return (time.time() - last) < SCAN_INTERVAL_SECONDS * 3


def get_schedule(db: Session, task_id: str,
                 operator: Operator | None = None) -> Schedule:
    row = db.query(Schedule).filter(Schedule.id == str(task_id)).one_or_none()
    if row is None:
        raise errors.not_found(f"定时任务 {task_id} 不存在", taskId=str(task_id))
    _assert_operator_account_access(db, row.account_id, operator)
    _assert_schedule_owner(row, operator)
    return row


def _verify_shop(db: Session, account_id: int | None, shop_id: str) -> None:
    if account_id is None:
        return
    account = get_account(db, account_id)
    if account is None:
        raise errors.not_found(f"账号 {account_id} 不存在", accountId=str(account_id))
    require_shop(db, account, shop_id)


def _verify_template(db: Session, template: str, *, required: bool = False) -> None:
    from services import setting_service

    if not str(template or "").strip():
        if required:
            # 创建时必须绑模板：否则任务能建出来，运行期才在 publish 里报
            # TEMPLATE_INVALID，把配错推迟到发布时刻，当天那次就白跑了。
            raise errors.validation("请选择用哪套定价模板", field="template")
        # 局部更新允许暂时不绑模板（例如只改日期），发布时按「不使用模板」处理
        return
    if template not in setting_service.price_templates(db):
        raise errors.not_found(f"定价模板「{template}」不存在", template=template)


def create(db: Session, payload: Mapping[str, Any],
           operator: Operator | None = None) -> dict[str, Any]:
    fields = normalize(db, payload)
    _assert_operator_account_access(db, fields["account_id"], operator)
    _assert_member_schedule_mode(operator, fields["claim_only"])
    if not fields["selected_product_ids"]:
        raise errors.validation("请至少选择一件要发布的仓库商品", field="selectedProductIds")
    eligible = pick_products(
        db, account_id=fields["account_id"], count=len(fields["selected_product_ids"]),
        claim_only=fields["claim_only"], operator_id=operator.id if operator else None,
        selected_product_ids=fields["selected_product_ids"])
    if len(eligible) != len(fields["selected_product_ids"]):
        raise errors.validation("部分所选商品不可用于该账号发布，请刷新商品列表后重选",
                                field="selectedProductIds")
    _verify_template(db, fields["template_name"], required=True)
    _verify_shop(db, fields["account_id"], fields["shop_id"])
    row = Schedule(id=f"task-{uuid.uuid4().hex[:12]}", created_at=now_ts(),
                   updated_at=now_ts(),
                   last_run_date="", attempt_date="", attempts=0,
                   next_due_at=None, last_result={},
                   owner_operator_id=(operator.id if operator is not None else None))
    for key, value in fields.items():
        setattr(row, key, value)
    row.completed = False
    due = next_due(as_dict(row), datetime.now())
    row.next_due_at = due.timestamp() if due else None
    db.add(row)
    db.flush()
    account = get_account(db, row.account_id)
    return {"task": serialize(row, account_name=account.name if account else "",
                             shop_name=_shop_name_of(db, account, row.shop_id))}


def update(db: Session, task_id: str, payload: Mapping[str, Any],
           operator: Operator | None = None) -> dict[str, Any]:
    if not payload:
        raise errors.validation("请求体不能为空，请至少提供一个要修改的字段")
    row = get_schedule(db, task_id, operator=operator)
    fields = normalize(db, payload, partial=True, existing=row)
    _assert_operator_account_access(db, fields["account_id"], operator)
    _assert_member_schedule_mode(operator, fields["claim_only"])
    _verify_template(db, fields["template_name"])
    _verify_shop(db, fields["account_id"], fields["shop_id"])
    for key, value in fields.items():
        setattr(row, key, value)
    if payload.get("enabled") is True or "selectedProductIds" in payload:
        row.completed = False
    if "selectedProductIds" in payload:
        row.completed_runs = 0
    due = next_due(as_dict(row), datetime.now())
    row.next_due_at = due.timestamp() if due else None
    row.updated_at = now_ts()
    db.flush()
    account = get_account(db, row.account_id)
    return {"task": serialize(row, account_name=account.name if account else "",
                             shop_name=_shop_name_of(db, account, row.shop_id))}


def delete(db: Session, task_id: str,
           operator: Operator | None = None) -> dict[str, Any]:
    from models.publish_log import PublishLog

    row = get_schedule(db, task_id, operator=operator)
    kept = db.query(PublishLog).filter(PublishLog.schedule_id == row.id).update(
        {PublishLog.schedule_id: None}, synchronize_session=False)
    db.delete(row)
    db.flush()
    return {"deleted": True, "id": str(task_id), "logsKept": int(kept or 0)}


def note_result(db: Session, task_id: str, *, success: bool, result: dict[str, Any],
                moment: datetime | None = None) -> Schedule:
    """记录一次执行结果：成功标记当天已跑，失败累加当天次数。

    `moment` 必须由调用方给（run_due 传它自己的 now）：否则「为什么判定到期」
    用的是传入的 now，「今天跑没跑」用的是 date.today()，跨零点或补跑历史时刻
    时两边不是同一天，会重复跑或漏跑。
    """
    row = get_schedule(db, task_id)
    today = (moment or datetime.now()).date().isoformat()
    if success:
        row.last_run_date = today
        row.attempt_date = today
        row.attempts = 0
    else:
        if (row.attempt_date or "") != today:
            row.attempt_date = today
            row.attempts = 0
        row.attempts = int(row.attempts or 0) + 1
    row.last_result = {**result, "at": now_ts()}
    row.last_run_at = now_ts()
    due = next_due(as_dict(row), datetime.now())
    row.next_due_at = due.timestamp() if due else None
    row.updated_at = now_ts()
    db.flush()
    return row


def pick_products(db: Session, *, account_id: int | None, count: int,
                  template: str = "", claim_only: bool = False,
                  operator_id: int | None = None,
                  selected_product_ids: list[str] | None = None,
                  run_index: int = 0
                  ) -> list[dict[str, Any]]:
    """挑出本轮要处理的商品，按 saved_at 倒序，最多 per_run 件。

    学员按本人认领和目标账号的发布记录选共享成品，不按成品来源账号或全局
    发布状态过滤。已发布过的不再取；数量不足时少取，不报错。
    """
    from models.product import Product
    from models.product_claim import ProductClaim

    query = db.query(Product).filter(Product.deleted_at.is_(None))
    if selected_product_ids is not None:
        start = max(run_index, 0) * count
        ids = selected_product_ids[start:start + count]
        if not ids:
            return []
        # Product.id 是 Integer；前端传来的 id 是字符串，
        # 直接 in_(["308"]) 会被 psycopg 绑定成 ::VARCHAR，报
        # 「operator does not exist: integer = character varying」。
        numeric_ids = []
        for value in ids:
            try:
                numeric_ids.append(int(str(value).strip()))
            except (TypeError, ValueError):
                continue
        if not numeric_ids:
            return []
        query = query.filter(Product.id.in_(numeric_ids))
    operator = (operator_service.get(db, operator_id)
                if operator_id is not None else None)
    if (operator is not None
            and operator_service.role_of(operator) == operator_service.ROLE_MEMBER):
        if claim_only:
            raise errors.AppError(errors.FORBIDDEN,
                                  "学员定时任务只能发布已认领的成品商品",
                                  status=403)
        _assert_operator_account_access(db, account_id, operator)
        # 共享仓库商品对所有学员可见，但每个学员只能排自己认领过的。
        # ProductClaim.product_id 与 Product.id 都是 Integer，直接子查询即可。
        # 之前误加了 cast(..., Text)，反而造成 integer = text 类型不匹配，
        # 报「operator does not exist: integer = text」。
        claimed_ids = (db.query(ProductClaim.product_id)
                       .filter(ProductClaim.owner_operator_id == operator.id,
                               ProductClaim.status == "claimed"))
        query = query.filter(Product.id.in_(claimed_ids),
                             Product.automation_result.is_(False),
                             Product.status != "archived")
    else:
        # 成品仓库是所有账号共享的货源，不属于任何一个人（负责人 2026-11-07 明确）：
        # 任何账号都能认领、都能把它发到自己的店铺。
        # 所以**不再**按 Product.account_id 过滤 —— 那会把「A 账号采进来的商品」
        # 锁死成只有 A 能发，换账号排定时任务就报「部分所选商品不可用于该账号发布」。
        #
        # 下面这个 status != published 的过滤也只在「自动轮转」（未显式勾选）时生效：
        # 显式勾选的商品是运营点名要发的，哪怕之前发过（换店铺 / 上次失败要重发）
        # 也必须允许，否则用户看到的就是「明明选好了却说不可用」。
        explicit = bool(selected_product_ids)
        if not claim_only and not explicit:
            query = query.filter(Product.status != "published")
    published = _published_identities(db, account_id, operator_id)
    # 显式选定的商品不再按「是否发布过」过滤（负责人 2026-11-06 明确）：
    # 运营手动勾选就是要发这几件，哪怕之前发过一次（可能有多个店铺、
    # 或上次失败要重发）。自动轮转（未指定商品）才需要跳过已发布的，
    # 否则每轮都会撞同一批。
    skip_published = not (selected_product_ids is not None
                          and len(selected_product_ids) > 0)
    # 分页扫完全部候选，而不是只取最近 count*4 件。
    # 只取一页时，若最近那批都已发布（很常见：连着几天跑过），本轮就取到 0 件，
    # 更早的未发布商品永远轮不到——表现为「定时任务明明有货却一直跳过」。
    picked: list[dict[str, Any]] = []
    page_size = max(count * 4, 50)
    offset = 0
    rows: list[Any] = []
    while len(picked) < count:
        batch = (query.order_by(Product.saved_at.desc())
                 .limit(page_size).offset(offset).all())
        if not batch:
            break
        rows.extend(batch)
        offset += page_size
        # 只有在扫完这一页仍不够时才继续翻页
        for row in batch:
            if skip_published:
                if str(row.detail_id) in published["detail_ids"]:
                    continue
                if str(row.warehouse_key) in published["keys"]:
                    continue
            picked.append({
                "id": row.id,
                "detailId": row.detail_id,
                "shopId": row.shop_id,
                "warehouseKey": row.warehouse_key,
                "title": row.title or "",
                "payload": row.payload if isinstance(row.payload, dict) else {},
            })
            if len(picked) >= count:
                break
        if offset > 100000:  # 兜底，防止数据异常时无限翻页
            break
    return picked


def _published_identities(db: Session, account_id: int | None,
                          operator_id: int | None) -> dict[str, set[str]]:
    from models.publish_log import PublishLog

    query = db.query(PublishLog).filter(PublishLog.status.in_(("success", "processing")))
    if account_id is not None:
        query = query.filter(PublishLog.account_id == account_id)
    # A shared Miaoshou account can have several software-user ledgers. Match
    # only this schedule owner's records; NULL remains the isolated legacy
    # ledger used by ownerless schedules and must not leak into member tasks.
    query = query.filter(PublishLog.operator_id == operator_id)
    detail_ids: set[str] = set()
    keys: set[str] = set()
    for row in query.all():
        if row.detail_id:
            detail_ids.add(str(row.detail_id))
        if row.warehouse_key:
            keys.add(str(row.warehouse_key))
    return {"detail_ids": detail_ids, "keys": keys}


# --- 执行摘要（run_due 与手动执行共用） -----------------------------------
RUN_EMPTY = "empty"      # 选中 0 件
RUN_OK = "ok"            # 选中全部发布成功
RUN_SUBMITTED = "submitted"  # 已提交，等待平台最终回执
RUN_PARTIAL = "partial"  # 部分成功
RUN_FAILED = "failed"    # 全失败或抛异常


def _count(value: Any) -> int:
    """publish() 的 published / claimed / failures 都是 list，兼容旧的数字形态。"""
    if isinstance(value, (list, tuple, set)):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def run_status(*, selected: int, published: int, failed: int, submitted: int = 0,
               error: str = "") -> str:
    """Accepted submissions stay distinct from final successful receipts."""
    if error:
        return RUN_FAILED
    if int(selected) <= 0:
        return RUN_EMPTY
    if int(submitted) > 0:
        return RUN_PARTIAL if int(failed) > 0 else RUN_SUBMITTED
    if int(published) <= 0:
        return RUN_FAILED
    if int(failed) > 0 or int(published) < int(selected):
        return RUN_PARTIAL
    return RUN_OK


def _default_reason(status: str, *, requested: int, selected: int,
                    published: int, failed: int, submitted: int = 0,
                    account_has_products: bool | None = None,
                    warehouse_empty: bool | None = None) -> str:
    if status == RUN_EMPTY:
        # 三种「没货」要分开报：都发完了 / 账号隔离选错 / 仓库真的是空的。
        # 一句「没有可处理的商品」用户无法判断要不要去改账号配置。
        if warehouse_empty:
            return "仓库是空的，先去采集箱处理商品"
        if account_has_products is False:
            return "仓库里没有属于这个账号的商品，检查任务的账号设置"
        return "这个账号在仓库里没有可发布的新商品（都已发布过）"
    if status == RUN_OK:
        return ""
    if status == RUN_SUBMITTED:
        return f"已提交 {submitted} 件，等待平台回执；请在发布记录中刷新核对"
    if status == RUN_PARTIAL:
        return (f"计划 {requested} 件，实际选中 {selected} 件，"
                f"成功 {published} 件，待回执 {submitted} 件，失败 {failed} 件")
    return f"选中 {selected} 件全部发布失败"


def summarize(*, task_id: str, requested: int, selected: int, published: int,
              failures: int = 0, submitted: int = 0, reason: str = "",
              results: list[dict[str, Any]] | None = None,
              error: str = "",
              account_has_products: bool | None = None,
              warehouse_empty: bool | None = None) -> dict[str, Any]:
    """统一执行摘要字段：requested / selected / published / failed / status / reason。

    requested = 计划的 perRun（本轮最多取多少件，不是必须发满）；
    selected  = 实际选中的件数；
    submitted = 已提交、尚无最终回执的件数，不算成功或失败；
    failed    = 实际失败数。failures 与未成功且未提交的差额是同一批失败件的两种
                口径，取两者最大值而不是相加，否则「2 选 1 成 1 败」会被算成 2 件失败；
                上限不超过 selected；抛异常时按选中的件全部失败计。
                绝不用 len(items) 当失败数。
    """
    requested = max(int(requested), 0)
    selected = max(int(selected), 0)
    published = min(max(int(published), 0), selected)
    submitted = min(max(int(submitted), 0), selected - published)
    if error:
        failed = selected
        status = run_status(selected=selected, published=published, failed=failed,
                            error=error)
        reason = reason or error
    else:
        raw_failed = max(int(failures), selected - published - submitted, 0)
        failed = min(raw_failed, selected) if selected else 0
        status = run_status(selected=selected, published=published, failed=failed,
                            submitted=submitted)
        if not reason:
            reason = _default_reason(status, requested=requested, selected=selected,
                                     published=published, failed=failed, submitted=submitted,
                                     account_has_products=account_has_products,
                                     warehouse_empty=warehouse_empty)
    return {
        "taskId": str(task_id),
        "status": status,
        "requested": requested,
        "selected": selected,
        "published": published,
        "submitted": submitted,
        "failed": failed,
        "count": published,  # 兼容旧前端字段
        "reason": reason,
        "results": list(results or []),
        "at": None,
    }


def _results_of(outcome: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把 publish() 的回执摊平成逐条结果（成功 + 失败）。"""
    rows: list[dict[str, Any]] = []
    records = outcome.get("records")
    items = records.get("items") if isinstance(records, Mapping) else None
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        rows.append({"logId": item.get("logId"), "detailId": item.get("detailId"),
                     "status": item.get("status") or "processing",
                     "title": item.get("title"), "reason": item.get("reason") or ""})
    for failure in outcome.get("failures") or []:
        if not isinstance(failure, Mapping):
            continue
        rows.append({"detailId": failure.get("detailId"), "status": "failed",
                     "reason": failure.get("reason")})
    return rows


def _log_summary(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _publish_summary(outcome: Mapping[str, Any], *, task: Schedule, requested: int,
                     selected: int, batch_id: str, with_results: bool) -> dict[str, Any]:
    records = outcome.get("records")
    items = records.get("items") if isinstance(records, Mapping) else []
    items = [item for item in items or [] if isinstance(item, Mapping)]
    claim_only = bool(task.claim_only)
    published = (_count(outcome.get("claimed")) if claim_only else
                 sum(item.get("status") == "success" for item in items))
    submitted = (0 if claim_only else
                 sum(item.get("status") == "processing" for item in items)
                 if items else _count(outcome.get("published")))
    failures = _count(outcome.get("failures"))
    summary = summarize(
        task_id=str(task.id), requested=requested, selected=selected,
        published=published, submitted=submitted,
        failures=failures + sum(item.get("status") in {"fail", "cancel"} for item in items),
        results=_results_of(outcome) if with_results else [])
    if not claim_only:
        summary.update({
            "batchId": batch_id,
            "submissionFailures": failures,
            "publishStatuses": {str(item["logId"]): str(item.get("status") or "processing")
                                for item in items if item.get("logId") is not None},
        })
    return summary


def _apply_receipt(summary: dict[str, Any], log: Any) -> dict[str, Any] | None:
    statuses = summary.get("publishStatuses")
    if (summary.get("batchId") != log.batch_id or not isinstance(statuses, dict)
            or str(log.id) not in statuses):
        return None
    statuses = {**statuses, str(log.id): log.status}
    result = summarize(
        task_id=str(summary.get("taskId") or ""), requested=int(summary.get("requested") or 0),
        selected=int(summary.get("selected") or 0),
        published=sum(status == "success" for status in statuses.values()),
        submitted=sum(status == "processing" for status in statuses.values()),
        failures=int(summary.get("submissionFailures") or 0)
        + sum(status in {"fail", "cancel"} for status in statuses.values()),
        results=[{**item, "status": log.status, "reason": str(log.reason or "")}
                 if str(item.get("logId")) == str(log.id) else item
                 for item in summary.get("results") or []],
    )
    return {**summary, **result, "at": summary.get("at"), "publishStatuses": statuses}


def reconcile_publish_log(db: Session, log: Any) -> bool:
    """Update only the schedule execution identified by this exact final receipt.

    Each execution has its own batch ID. A later retry may reuse a PublishLog
    row, but must not rewrite an older execution's history or its owner's data.
    """
    if log.status not in {"success", "fail", "cancel"} or not log.schedule_id:
        return False
    from models.schedule_log import ScheduleLog

    changed = False
    completed_run_index: int | None = None
    rows = (db.query(ScheduleLog).filter(
        ScheduleLog.task_id == log.schedule_id,
        ScheduleLog.account_id == log.account_id,
        ScheduleLog.owner_operator_id == log.operator_id,
    ).order_by(ScheduleLog.at.desc()).limit(LOG_RETAIN).all())
    for row in rows:
        result = _apply_receipt(_log_summary(row.summary), log)
        if result is None:
            continue
        row.status = result["status"]
        row.reason = str(result["reason"])[:500]
        row.published = result["published"]
        row.failed = result["failed"]
        row.summary = json.dumps(result, ensure_ascii=False, default=str)
        changed = True
        if result["status"] == RUN_OK and "scheduledRunIndex" in result:
            completed_run_index = int(result["scheduledRunIndex"])
        break
    task = db.query(Schedule).filter(
        Schedule.id == log.schedule_id,
        Schedule.account_id == log.account_id,
        Schedule.owner_operator_id == log.operator_id,
    ).one_or_none()
    if task is not None and isinstance(task.last_result, dict):
        result = _apply_receipt(task.last_result, log)
        if result is not None:
            task.last_result = result
            changed = True
            if result["status"] == RUN_OK and "scheduledRunIndex" in result:
                completed_run_index = int(result["scheduledRunIndex"])
    if (task is not None and completed_run_index is not None
            and task.selected_product_ids and task.planned_runs
            and completed_run_index == int(task.completed_runs or 0)):
        task.completed_runs = int(task.completed_runs or 0) + 1
        if task.completed_runs >= int(task.planned_runs):
            task.enabled = False
            task.completed = True
        task.updated_at = now_ts()
        changed = True
    return changed


def _store(db: Session, task: Schedule, summary: dict[str, Any],
           *, moment: datetime | None = None) -> dict[str, Any]:
    """记账：只有 failed 才累加当天重试次数，其余（含 partial）算当天已跑。"""
    saved = note_result(db, str(task.id), moment=moment,
                        success=summary["status"] in (RUN_OK, RUN_SUBMITTED),
                        result=summary)
    summary["at"] = (saved.last_result or {}).get("at")
    # 追加一条执行历史。`last_result` 只留最近一次，翻不出「昨天跑了几轮」。
    # 历史写失败不能影响发布结果本身的记账，所以单独吞掉。
    try:
        record_log(db, task, summary, moment=moment)
        if (task.selected_product_ids and task.planned_runs
                and summary.get("selected", 0) > 0
                and summary.get("status") == RUN_OK):
            task.completed_runs = int(task.completed_runs or 0) + 1
            if task.completed_runs >= int(task.planned_runs):
                task.enabled = False
                task.completed = True
                task.updated_at = now_ts()
    except Exception:  # noqa: BLE001
        logger.warning("写入定时任务执行历史失败 taskId=%s", task.id, exc_info=True)
    return summary


@contextmanager
def _batch_guard():
    """批次执行期间把词库置为「运行中」，退出时必定还原。

    为什么放在 execute 这一层：execute 是自动跑与手动跑（api/schedules.py）共用的
    唯一收口，包裹在这里，两条路径都不可能绕过闸门；而且 try/finally 保证业务异常、
    raise_on_error 的上抛、提前 return 都走同一条还原路径。

    为什么不是布尔：run_due 里多个账号线程同时进来，先结束的线程一写 False 就把
    别人的批次当成结束了。计数由 word_service 维护，归零才放开。
    """
    from services import word_service

    word_service.mark_building(True)
    try:
        yield
    finally:
        # 必须无条件执行：任何一条漏掉的 False 都会让词库永久处于「批次运行中」，
        # 用户从此改不了词库（兜底虽有 30 分钟，但那是事故后的补救，不是常态）。
        word_service.mark_building(False)


def _pending_receipt_log(db: Session, task: Schedule) -> Any | None:
    """本任务上一轮提交、平台回执还没回来的发布记录（没有就 None）。

    只认**同一个任务自己**留下的 processing 行：别人、别的任务挂着不影响本轮，
    那样才不会被「别人有一个待确认」连坐。
    """
    from models.publish_log import PublishLog
    from services import publish_service

    query = db.query(PublishLog).filter(
        PublishLog.account_id == task.account_id,
        PublishLog.schedule_id == str(task.id),
        PublishLog.status == "processing",
    )
    owner = task.owner_operator_id
    query = (query.filter(PublishLog.operator_id.is_(None)) if owner is None
             else query.filter(PublishLog.operator_id == owner))
    for row in query.order_by(PublishLog.id.desc()).all():
        # 认领（claim_only）行本来就没有发布回执，等它没有意义。
        if publish_service._is_claim_only_log(db, row):
            continue
        return row
    return None


def _store_waiting(db: Session, task: Schedule, *, requested: int, pending: Any,
                   moment: datetime | None = None) -> dict[str, Any]:
    """记一次「等回执」：不算一次执行，不动当天失败次数，也不写执行历史。

    为什么必须这样记：发布提交后平台回执要 1–3 分钟才建单（实测提交 19:28:27、
    平台记录 19:28:29，更早一次隔了约两分钟），而调度器每 60s 扫一轮。
    如果把「等回执」按失败记账，三次就吃满 SCHEDULE_MAX_ATTEMPTS_PER_DAY=3，
    任务当天直接不再执行 —— 用户看到的就是「定时任务设置好了不执行」。
    """
    summary = summarize(task_id=str(task.id), requested=requested, selected=0,
                        published=0, submitted=0)
    summary["status"] = RUN_SUBMITTED
    summary["reason"] = "上一轮发布还在等平台回执，本轮只等待（不计入失败次数）"
    summary["pendingLogId"] = str(getattr(pending, "id", "") or "")
    summary["pendingDetailId"] = str(getattr(pending, "detail_id", "") or "")
    summary["pendingRequestedAt"] = (getattr(pending, "last_requested_at", None)
                                     or getattr(pending, "requested_at", None))
    summary["at"] = now_ts()
    task.last_result = dict(summary)
    due = next_due(as_dict(task), datetime.now())
    task.next_due_at = due.timestamp() if due else None
    task.updated_at = now_ts()
    db.flush()
    return summary


def execute(db: Session, task: Schedule, *, raise_on_error: bool = False,
            with_results: bool = False, moment: datetime | None = None
            ) -> dict[str, Any]:
    """执行单个任务的一轮发布，返回统一摘要（不提交事务）。

    per_run 是「本轮最多取多少件」：选不满就是少发，status 照样可以是 ok。
    业务异常（AppError）一律折成 status=failed 的摘要返回；
    raise_on_error=True 时非业务异常向上抛（手动执行接口用），后台 worker 用默认值。
    """
    from services import setting_service

    # 只读模式必须挡住**自动**发布这条路径。老版 `--read-only` 是全局闸，
    # 定时任务也走同一道门；新版此前只在 HTTP 的写接口上校验，
    # 后台轮询一旦进入 execute 就会真的把商品发出去——用户以为「只读」是安全的。
    #
    # 这个判断必须在 _batch_guard 之前：只读分支什么都没动，没有任何理由去锁词库，
    # 置了位就白锁一轮（用户会在只读状态下莫名其妙改不了词库）。
    if setting_service.is_read_only(db):
        return {
            "status": RUN_EMPTY,
            "reason": "当前是只读模式，定时发布不会执行",
            "requested": int(task.per_run or 10),
            "selected": 0, "published": 0, "claimed": 0, "failed": 0,
            "failures": [], "results": [],
        }

    # 打「执行中」标记：发布一件 20-60 秒，没有这个标记界面只能看到上一轮结果，
    # 用户会以为任务根本没启动（2026-11-07 实测反馈）。
    mark_task_running(str(task.id))
    try:
        with _batch_guard():
            return _execute_locked(db, task, raise_on_error=raise_on_error,
                                   with_results=with_results, moment=moment)
    finally:
        clear_task_running(str(task.id))


def _execute_locked(db: Session, task: Schedule, *, raise_on_error: bool = False,
                    with_results: bool = False, moment: datetime | None = None
                    ) -> dict[str, Any]:
    """execute 的主体：进来时词库已置为「批次运行中」，这里只管跑。"""
    from services import account_service, publish_service

    requested = int(task.per_run or 10)
    selected = 0
    member_owner = False
    batch_id = f"sched-{uuid.uuid4().hex[:20]}"
    try:
        # 先把店铺核一遍：店铺在妙手里被删/换绑后，shopId 会失效，
        # 不校验就会在批次中途失败（前几件已发出、后几件全错），
        # 而且报的是通用 SHOP_NOT_IN_ACCOUNT，没有可操作信息。
        if task.shop_id:
            from models.account import Account

            account = (db.query(Account)
                       .filter(Account.id == task.account_id).one_or_none())
            if account is not None:
                account_service.require_shop(db, account, task.shop_id)
        if task.owner_operator_id is not None:
            owner = operator_service.get(db, task.owner_operator_id)
            member_owner = (
                operator_service.role_of(owner) == operator_service.ROLE_MEMBER)
            if member_owner and not owner.enabled:
                raise errors.forbidden(
                    "定时任务创建者已停用，后台定时发布已暂停")
            if (member_owner and not account_service.has_account_access(
                    db, task.account_id, owner.id)):
                raise errors.AppError(errors.FORBIDDEN,
                                      "定时任务创建者已无法访问所选妙手账号",
                                      status=403)
        # 上一轮提交的发布回执还没回来：这一轮什么都别做，但**不是失败**。
        # 按失败记账会吃满当天额度、把任务锁死一整天（详见 _store_waiting）。
        pending = _pending_receipt_log(db, task)
        if pending is not None:
            return _store_waiting(db, task, requested=requested,
                                  pending=pending, moment=moment)
        # 直接读 ORM 字段：as_dict() 只含调度判定需要的键，不含账号/店铺/模板
        items = pick_products(db, account_id=task.account_id, count=requested,
                              template=str(task.template_name or ""),
                              claim_only=bool(task.claim_only),
                              operator_id=task.owner_operator_id,
                              selected_product_ids=(list(task.selected_product_ids)
                                                    if task.selected_product_ids else None),
                              run_index=int(task.completed_runs or 0))
        selected = len(items)
        if not items:
            if task.selected_product_ids:
                task.completed = True
                task.enabled = False
                task.updated_at = now_ts()
                return _store(db, task, summarize(
                    task_id=str(task.id), requested=requested, selected=0,
                    published=0, reason="所选商品均已完成或不可发布，任务已结束"),
                    moment=moment)
            # 判断到底是「都发完了」还是「账号/仓库选错了」
            if member_owner:
                # Shared-account products and other students' claims are outside
                # this task owner's view. Avoid deriving the empty-state reason
                # from global or account-wide product existence.
                empty_summary = summarize(
                    task_id=str(task.id), requested=requested, selected=0,
                    published=0, reason="这个账号没有已认领且可发布的新商品")
            else:
                from models.product import Product

                warehouse_empty = (db.query(Product.id)
                                   .filter(Product.deleted_at.is_(None))
                                   .first() is None)
                account_has = None
                if not warehouse_empty:
                    account_has = task.account_id is not None and (
                        db.query(Product.id)
                        .filter(Product.deleted_at.is_(None),
                                Product.account_id == task.account_id)
                        .first() is not None)
                empty_summary = summarize(
                    task_id=str(task.id), requested=requested, selected=0,
                    published=0, account_has_products=account_has,
                    warehouse_empty=warehouse_empty)
            return _store(db, task, empty_summary, moment=moment)
        # publish_service.publish 的入参是请求体形状，不是散装参数
        outcome = publish_service.publish(db, {
            "confirm": True,
            "accountId": task.account_id,
            "shopId": task.shop_id,
            "template": task.template_name or None,
            "claimOnly": bool(task.claim_only),
            "items": [{"warehouseId": str(item["id"])} for item in items],
            "batchId": batch_id,
            "scheduleId": str(task.id),
        }, operator_id=task.owner_operator_id,
            _log_operator_id=task.owner_operator_id)
        summary = _publish_summary(
            outcome, task=task, requested=requested, selected=selected,
            batch_id=batch_id, with_results=with_results)
        if task.selected_product_ids:
            summary["scheduledRunIndex"] = int(task.completed_runs or 0)
        return _store(db, task, summary, moment=moment)
    except errors.AppError as exc:
        if exc.code == errors.UPSTREAM_UNKNOWN_RESULT:
            # The publish service commits its ledger before contacting the
            # platform. A lost response is pending, never a safe automatic retry.
            from models.publish_log import PublishLog

            rows = db.query(PublishLog).filter(
                PublishLog.account_id == task.account_id,
                PublishLog.operator_id == task.owner_operator_id,
                PublishLog.schedule_id == str(task.id),
                PublishLog.batch_id == batch_id,
            ).all()
            if rows:
                outcome = {"published": [row.detail_id for row in rows], "records": {
                    "items": [publish_service.serialize_log(row) for row in rows]}}
                summary = _publish_summary(
                    outcome, task=task, requested=requested, selected=selected,
                    batch_id=batch_id, with_results=with_results)
                summary["reason"] = exc.message
                if task.selected_product_ids:
                    summary["scheduledRunIndex"] = int(task.completed_runs or 0)
                return _store(db, task, summary, moment=moment)
        summary = summarize(task_id=str(task.id), requested=requested,
                            selected=selected, published=0, error=exc.message)
        summary["errorType"] = type(exc).__name__
        return _store(db, task, summary, moment=moment)
    except Exception as exc:  # noqa: BLE001 - 单个任务失败不影响其他任务
        summary = summarize(task_id=str(task.id), requested=requested,
                            selected=selected, published=0, error=str(exc)[:200])
        summary["errorType"] = type(exc).__name__
        if raise_on_error:
            raise
        return _store(db, task, summary, moment=moment)


# 一个账号卡死不该拖住整轮扫描
RUN_DUE_TIMEOUT_SECONDS = 1800


def refresh_processing_logs(db: Session, account_id: Any) -> int:
    """回源平台刷新 processing 状态的发布记录（后台自动做）。

    发布是异步的：提交成功只代表平台受理，真正的 success/fail 要回源才知道。
    如果没人点「刷新回执」，记录会一直停在 processing —— 下一轮定时发布
    就被 `_pending_publish_blocker` 拦住，表现为「任务到点不执行」。
    这里在每轮扫描前自动回源一次，让状态自己走完。
    """
    try:
        from api.publish import _refresh_processing

        result = _refresh_processing(db, str(account_id) if account_id else None)
        updated = int(result.get("updated") or 0)
        if updated:
            logger.info("定时扫描前刷新回执：%d 条", updated)
        return updated
    except Exception as exc:  # noqa: BLE001 - 刷新失败不阻断扫描
        logger.warning("定时扫描前刷新回执失败：%s", exc)
        return 0


def release_stale_publish_logs(db: Session, *, max_age_seconds: int = 600) -> int:
    """释放长时间停在 processing 的发布记录。

    发布是异步的，平台回执要稍后刷新。没人刷新就会永久停在 processing，
    而 `_pending_publish_blocker` 看到 processing 就拒绝再次发布——
    表现就是「定时任务到点不执行」或「手动发布报结果尚未确认」。
    这在实际使用里必然发生（网络断、页面关掉、服务重启），
    所以每轮扫描前先把过期的放掉，标 unknown（不假装成功）。
    """
    import time as _time

    from models.publish_log import PublishLog

    now = _time.time()
    rows = (db.query(PublishLog)
            .filter(PublishLog.status == "processing").all())
    released = 0
    for row in rows:
        stamp = row.last_requested_at or row.requested_at or 0
        age = now - float(stamp) if stamp else max_age_seconds + 1
        if age <= max_age_seconds:
            continue
        row.status = "unknown"
        row.reason = "超时未收到平台回执，已释放以免阻塞后续发布"
        row.finished_at = now
        released += 1
    if released:
        db.commit()
        logger.info("释放 %d 条超时未回执的发布记录", released)
    return released


def run_due(db: Session, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """扫描到期的定时任务并执行一轮发布。

    按账号分组、一账号一线程并行，同账号内串行——限流是按账号算的，
    不同账号并行安全，而串行会让多账号同时到点的总耗时线性叠加。
    每轮最多取 per_run 件（是上限，不是必须发满）；失败最多重试 3 次。

    每个线程用**自己的会话**：SQLAlchemy 的 Session 不跨线程。
    """
    moment = now or datetime.now()
    # 先回源刷新 + 释放卡住的记录，否则 _pending_publish_blocker 会把到点任务全拦下。
    # 顺序：先刷新（能拿到真实结果的走 success/fail），再释放（拿不到的标 unknown）。
    try:
        account_ids = {row.account_id for row in
                       db.query(Schedule).filter(Schedule.enabled.is_(True)).all()}
        for account_id in account_ids:
            refresh_processing_logs(db, account_id)
        release_stale_publish_logs(db)
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻断扫描
        logger.warning("刷新/释放发布记录失败：%s", exc)
    tasks = due_tasks(db, moment)
    if not tasks:
        return []
    # 先把主会话的活干完（due_tasks 可能已经触发 lazy load），再交给线程
    grouped: dict[Any, list[Any]] = {}
    for task in tasks:
        grouped.setdefault(task.account_id, []).append(task)
    plans = [(account_id, [str(task.id) for task in group])
             for account_id, group in grouped.items()]

    if len(plans) == 1:
        # 单账号没必要开线程，省掉一次会话切换
        account_id, task_ids = plans[0]
        return _run_plan(db, task_ids, moment)

    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import wait as wait_futures

    summaries: list[dict[str, Any]] = []
    # 整轮也占一次计数：worker 用 wait(timeout) 提前放弃等待时，被放弃的线程
    # 还在跑，但 run_due 已经把结果返回给调度器了——外面看起来「这轮结束了」，
    # 词库却还能被改。多这一层，最外层的 False 要等本函数真正退出才减。
    # 内层每个 execute 自己也是成对加减，所以嵌套不会算错（0→1 才记时间戳）。
    with _batch_guard(), ThreadPoolExecutor(max_workers=len(plans),
                            thread_name_prefix="sched") as pool:
        futures = [pool.submit(_run_plan_in_thread, task_ids, moment)
                   for _account_id, task_ids in plans]
        done, not_done = wait_futures(futures, timeout=RUN_DUE_TIMEOUT_SECONDS)
        for future in done:
            try:
                summaries.extend(future.result())
            except Exception as exc:  # noqa: BLE001 - 单账号失败不影响其他
                logger.exception("定时发布账号线程失败：%s", exc)
        for future in not_done:
            logger.error("定时发布账号线程超时（%ss），已放弃等待",
                         RUN_DUE_TIMEOUT_SECONDS)
            future.cancel()
    return summaries


def _run_plan_in_thread(task_ids: list[str],
                       moment: datetime | None = None) -> list[dict[str, Any]]:
    """线程入口：开自己的会话跑这一组任务。

    外层再包一次计数，是因为 _run_plan 返回后 session_scope 还要提交一次事务：
    提交期间这一批的写入还没落盘，此时放开闸门，用户改词库就会和这批回执打架。
    """
    from db.session import session_scope

    with _batch_guard(), session_scope() as own_db:
        return _run_plan(own_db, task_ids, moment)


def _run_plan(db: Session, task_ids: list[str],
              moment: datetime | None = None) -> list[dict[str, Any]]:
    """在给定会话上按顺序执行一组任务（同账号串行）。

    自带一次计数：execute 之间还有 db.query 和 db.commit（提交回执、写执行历史），
    这些也属于「一轮批次」的一部分，把缝隙露出来同样会出现「批次还在收尾、
    词库已经能改」的窗口。
    """
    summaries: list[dict[str, Any]] = []
    with _batch_guard():
        for task_id in task_ids:
            task = db.query(Schedule).filter(Schedule.id == task_id).one_or_none()
            if task is None:
                continue
            summaries.append(execute(db, task, moment=moment))
            db.commit()
    return summaries
