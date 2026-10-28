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
from models.schedule import Schedule
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
        raise errors.validation("频率只能是每天 / 每周 / 每月 / 指定日期",
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
        if not 1 <= per_run <= 200:
            raise errors.validation("每次发布数量必须在 1 到 200 之间",
                                    field="perRun", value=per_run)

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
        "claim_only": bool(pick("claimOnly") if "claimOnly" in provided else
                           (existing.claim_only if existing else False)),
    }


# --- 到期判断（纯函数） ---------------------------------------------------
def is_scheduled_day(task: Mapping[str, Any], day: date) -> bool:
    frequency = task.get("frequency")
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
        "claimOnly": bool(row.claim_only),
        "lastRunDate": row.last_run_date or "",
        "attemptDate": row.attempt_date or "",
        "attempts": int(row.attempts or 0),
        "nextDueAt": (due.timestamp() if due else None),
        "nextDueText": (due.strftime("%Y-%m-%d %H:%M") if due else "已停用"),
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
        "lastResult": row.last_result if isinstance(row.last_result, dict) else {},
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
        status=str(summary.get("status") or ""),
        reason=str(summary.get("reason") or "")[:500],
        requested=int(summary.get("requested") or 0),
        selected=int(summary.get("selected") or 0),
        published=int(summary.get("published") or 0),
        failed=int(summary.get("failed") or 0),
        summary=json.dumps(summary, ensure_ascii=False, default=str)[:20000],
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


def list_logs(db: Session, *, task_id: str = "", limit: Any = LOG_LIMIT_DEFAULT
              ) -> dict[str, Any]:
    """执行历史，新的在前。"""
    from models.schedule_log import ScheduleLog

    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = LOG_LIMIT_DEFAULT
    count = max(1, min(LOG_LIMIT_MAX, count))
    query = db.query(ScheduleLog)
    if str(task_id or "").strip():
        query = query.filter(ScheduleLog.task_id == str(task_id).strip())
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
            "at": row.at,
        } for row in rows],
        "total": total,
        "limit": count,
    }


# --- CRUD -----------------------------------------------------------------
def list_schedules(db: Session, *, account_id: Any = None,
                   enabled: bool | None = None) -> dict[str, Any]:
    query = db.query(Schedule)
    if account_id not in (None, ""):
        text = str(account_id)
        if text.isdigit():
            query = query.filter(Schedule.account_id == int(text))
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


def get_schedule(db: Session, task_id: str) -> Schedule:
    row = db.query(Schedule).filter(Schedule.id == str(task_id)).one_or_none()
    if row is None:
        raise errors.not_found(f"定时任务 {task_id} 不存在", taskId=str(task_id))
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


def create(db: Session, payload: Mapping[str, Any]) -> dict[str, Any]:
    fields = normalize(db, payload)
    _verify_template(db, fields["template_name"], required=True)
    _verify_shop(db, fields["account_id"], fields["shop_id"])
    row = Schedule(id=f"task-{uuid.uuid4().hex[:12]}", created_at=now_ts(),
                   updated_at=now_ts(),
                   last_run_date="", attempt_date="", attempts=0,
                   next_due_at=None, last_result={})
    for key, value in fields.items():
        setattr(row, key, value)
    due = next_due(as_dict(row), datetime.now())
    row.next_due_at = due.timestamp() if due else None
    db.add(row)
    db.flush()
    account = get_account(db, row.account_id)
    return {"task": serialize(row, account_name=account.name if account else "",
                             shop_name=_shop_name_of(db, account, row.shop_id))}


def update(db: Session, task_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not payload:
        raise errors.validation("请求体不能为空，请至少提供一个要修改的字段")
    row = get_schedule(db, task_id)
    fields = normalize(db, payload, partial=True, existing=row)
    _verify_template(db, fields["template_name"])
    _verify_shop(db, fields["account_id"], fields["shop_id"])
    for key, value in fields.items():
        setattr(row, key, value)
    due = next_due(as_dict(row), datetime.now())
    row.next_due_at = due.timestamp() if due else None
    row.updated_at = now_ts()
    db.flush()
    account = get_account(db, row.account_id)
    return {"task": serialize(row, account_name=account.name if account else "",
                             shop_name=_shop_name_of(db, account, row.shop_id))}


def delete(db: Session, task_id: str) -> dict[str, Any]:
    from models.publish_log import PublishLog

    row = get_schedule(db, task_id)
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
                  template: str = "", claim_only: bool = False
                  ) -> list[dict[str, Any]]:
    """挑出本轮要处理的商品：该账号、未发布、按 saved_at 倒序，最多 per_run 件。

    已发布过的（按 detailId 与 warehouseKey 双重判重）不再取；数量不足时少取，
    不报错（少跑可补救，错跑不可撤销）。
    """
    from models.product import Product

    query = db.query(Product).filter(Product.deleted_at.is_(None))
    if account_id is not None:
        query = query.filter(Product.account_id == account_id)
    if not claim_only:
        query = query.filter(Product.status != "published")
    published = _published_identities(db, account_id)
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


def _published_identities(db: Session, account_id: int | None) -> dict[str, set[str]]:
    from models.publish_log import PublishLog

    query = db.query(PublishLog).filter(PublishLog.status.in_(("success", "processing")))
    if account_id is not None:
        query = query.filter(PublishLog.account_id == account_id)
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


def run_status(*, selected: int, published: int, failed: int, error: str = "") -> str:
    """四态判定（纯函数）：empty / ok / partial / failed。"""
    if error:
        return RUN_FAILED
    if int(selected) <= 0:
        return RUN_EMPTY
    if int(published) <= 0:
        return RUN_FAILED
    if int(failed) > 0 or int(published) < int(selected):
        return RUN_PARTIAL
    return RUN_OK


def _default_reason(status: str, *, requested: int, selected: int,
                    published: int, failed: int,
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
    if status == RUN_PARTIAL:
        return (f"计划 {requested} 件，实际选中 {selected} 件，"
                f"成功 {published} 件，失败 {failed} 件")
    return f"选中 {selected} 件全部发布失败"


def summarize(*, task_id: str, requested: int, selected: int, published: int,
              failures: int = 0, reason: str = "",
              results: list[dict[str, Any]] | None = None,
              error: str = "",
              account_has_products: bool | None = None,
              warehouse_empty: bool | None = None) -> dict[str, Any]:
    """统一执行摘要字段：requested / selected / published / failed / status / reason。

    requested = 计划的 perRun（本轮最多取多少件，不是必须发满）；
    selected  = 实际选中的件数；
    failed    = 实际失败数。failures 与 (selected - published) 是同一批失败件的两种
                口径，取两者最大值而不是相加，否则「2 选 1 成 1 败」会被算成 2 件失败；
                上限不超过 selected；抛异常时按选中的件全部失败计。
                绝不用 len(items) 当失败数。
    """
    requested = max(int(requested), 0)
    selected = max(int(selected), 0)
    published = min(max(int(published), 0), selected)
    if error:
        failed = selected
        status = run_status(selected=selected, published=published, failed=failed,
                            error=error)
        reason = reason or error
    else:
        raw_failed = max(int(failures), selected - published, 0)
        failed = min(raw_failed, selected) if selected else 0
        status = run_status(selected=selected, published=published, failed=failed)
        if not reason:
            reason = _default_reason(status, requested=requested, selected=selected,
                                     published=published, failed=failed,
                                     account_has_products=account_has_products,
                                     warehouse_empty=warehouse_empty)
    return {
        "taskId": str(task_id),
        "status": status,
        "requested": requested,
        "selected": selected,
        "published": published,
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
        rows.append({"detailId": item.get("detailId"), "status": "published",
                     "title": item.get("title")})
    for failure in outcome.get("failures") or []:
        if not isinstance(failure, Mapping):
            continue
        rows.append({"detailId": failure.get("detailId"), "status": "failed",
                     "reason": failure.get("reason")})
    return rows


def _store(db: Session, task: Schedule, summary: dict[str, Any],
           *, moment: datetime | None = None) -> dict[str, Any]:
    """记账：只有 failed 才累加当天重试次数，其余（含 partial）算当天已跑。"""
    saved = note_result(db, str(task.id), moment=moment,
                        success=summary["status"] != RUN_FAILED, result=summary)
    summary["at"] = (saved.last_result or {}).get("at")
    # 追加一条执行历史。`last_result` 只留最近一次，翻不出「昨天跑了几轮」。
    # 历史写失败不能影响发布结果本身的记账，所以单独吞掉。
    try:
        record_log(db, task, summary, moment=moment)
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

    with _batch_guard():
        return _execute_locked(db, task, raise_on_error=raise_on_error,
                               with_results=with_results, moment=moment)


def _execute_locked(db: Session, task: Schedule, *, raise_on_error: bool = False,
                    with_results: bool = False, moment: datetime | None = None
                    ) -> dict[str, Any]:
    """execute 的主体：进来时词库已置为「批次运行中」，这里只管跑。"""
    from services import publish_service

    requested = int(task.per_run or 10)
    selected = 0
    try:
        # 先把店铺核一遍：店铺在妙手里被删/换绑后，shopId 会失效，
        # 不校验就会在批次中途失败（前几件已发出、后几件全错），
        # 而且报的是通用 SHOP_NOT_IN_ACCOUNT，没有可操作信息。
        if task.shop_id:
            from models.account import Account
            from services import account_service

            account = (db.query(Account)
                       .filter(Account.id == task.account_id).one_or_none())
            if account is not None:
                account_service.require_shop(db, account, task.shop_id)
        # 直接读 ORM 字段：as_dict() 只含调度判定需要的键，不含账号/店铺/模板
        items = pick_products(db, account_id=task.account_id, count=requested,
                              template=str(task.template_name or ""),
                              claim_only=bool(task.claim_only))
        selected = len(items)
        if not items:
            # 判断到底是「都发完了」还是「账号/仓库选错了」
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
            return _store(db, task, summarize(
                task_id=str(task.id), requested=requested, selected=0, published=0,
                account_has_products=account_has, warehouse_empty=warehouse_empty),
                moment=moment)
        # publish_service.publish 的入参是请求体形状，不是散装参数
        outcome = publish_service.publish(db, {
            "confirm": True,
            "accountId": task.account_id,
            "shopId": task.shop_id,
            "template": task.template_name or None,
            "claimOnly": bool(task.claim_only),
            "items": [{"warehouseId": str(item["id"])} for item in items],
            "batchId": f"sched-{task.id}",
            "scheduleId": str(task.id),
        })
        published = _count(outcome.get("published"))
        if not published and bool(task.claim_only):
            # 认领模式没有 published，成功数是 claimed
            published = _count(outcome.get("claimed"))
        summary = summarize(task_id=str(task.id), requested=requested,
                            selected=selected, published=published,
                            failures=_count(outcome.get("failures")),
                            results=(_results_of(outcome) if with_results else []))
        return _store(db, task, summary, moment=moment)
    except errors.AppError as exc:
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


def run_due(db: Session, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """扫描到期的定时任务并执行一轮发布。

    按账号分组、一账号一线程并行，同账号内串行——限流是按账号算的，
    不同账号并行安全，而串行会让多账号同时到点的总耗时线性叠加。
    每轮最多取 per_run 件（是上限，不是必须发满）；失败最多重试 3 次。

    每个线程用**自己的会话**：SQLAlchemy 的 Session 不跨线程。
    """
    moment = now or datetime.now()
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
