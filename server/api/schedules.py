"""定时发布路由。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from api.deps import CredentialsHeader, DbSession, OperatorDep, require_permission, require_writable
from api.helpers import parse_bool
from core import errors
from schemas.requests import ScheduleCreate, SchedulePatch
from services import account_service, audit_service, schedule_service

router = APIRouter(prefix="/schedules", tags=["定时发布"],
                   dependencies=[require_permission("schedule")])


def _task_of(result: Any) -> dict[str, Any]:
    """服务层返回的是 `{"task": {...}}`，审计要的字段在里层。

    单独摘出来是因为我在第一版里直接读了 `result.get("id")`：它永远 None，
    审计于是记了 target_id=""、name="" —— 表面上「写了审计」，实际上那条
    记录谁也定位不到。这种错在源码里看不出来，只有真打一次才发现。
    """
    if isinstance(result, dict) and isinstance(result.get("task"), dict):
        return result["task"]
    return result if isinstance(result, dict) else {}


@router.get("", summary="定时任务列表")
def list_schedules(db: DbSession,
    credentials: CredentialsHeader = None, accountId: str | None = None,
                   enabled: str | None = None) -> dict[str, Any]:
    flag = None if enabled in (None, "") else parse_bool(enabled, "enabled")
    return schedule_service.list_schedules(db, account_id=accountId, enabled=flag)


@router.post("", status_code=201, summary="新建定时任务")
def create_schedule(payload: ScheduleCreate, db: DbSession,
                    operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    body = payload.model_dump(by_alias=False, exclude_none=True)
    result = schedule_service.create(db, body)
    db.commit()
    # 定时任务是「会在半夜自己动别人商品」的东西：谁建的、建了哪条，
    # 必须留痕。target_kind="schedule" 让审计页拼出「定时任务「双十一」」。
    task = _task_of(result)
    audit_service.record(db, operator=operator, action="schedule.create",
                         target_kind="schedule",
                         target_id=str(task.get("id") or ""),
                         shop_id=str(task.get("shopId") or ""),
                         detail={"name": task.get("name") or "",
                                 "frequency": task.get("frequency") or "",
                                 "time": task.get("time") or "",
                                 "perRun": task.get("perRun") or 0})
    db.commit()
    return result


@router.patch("/{task_id}", summary="局部更新定时任务")
def patch_schedule(task_id: str, payload: SchedulePatch, db: DbSession,
                   operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    body = payload.model_dump(by_alias=False, exclude_unset=True)
    result = schedule_service.update(db, task_id, body)
    db.commit()
    # 记「改了哪些字段」而不是旧值新值：一条定时任务有十几个字段，
    # 全塞进 detail 只会让人懒得看。
    task = _task_of(result)
    audit_service.record(db, operator=operator, action="schedule.patch",
                         target_kind="schedule", target_id=str(task_id),
                         shop_id=str(task.get("shopId") or ""),
                         detail={"name": task.get("name") or "",
                                 "fields": sorted(body)})
    db.commit()
    return result


@router.delete("/{task_id}", summary="删除定时任务（保留历史记录）")
def delete_schedule(task_id: str, db: DbSession,
                    operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    # 名字要在删之前拿到：删完就查不到了，而审计必须答得出「删掉的那条叫什么」。
    # 这一步只是读，失败也不该挡住删除，所以单独吞掉异常。
    name = ""
    shop_id = ""
    try:
        row = schedule_service.get_schedule(db, task_id)
        name = str(row.name or "")
        shop_id = str(row.shop_id or "")
    except Exception:  # noqa: BLE001 - 名字取不到不影响删除本身
        pass
    with audit_service.guard(db, operator=operator, action="schedule.delete",
                             target_kind="schedule", target_id=str(task_id),
                             shop_id=shop_id, detail={"name": name}):
        result = schedule_service.delete(db, task_id)
    db.commit()
    audit_service.record(db, operator=operator, action="schedule.delete",
                         target_kind="schedule", target_id=str(task_id),
                         shop_id=shop_id,
                         detail={"name": name,
                                 "logsKept": result.get("logsKept") or 0})
    db.commit()
    return result


@router.get("/logs", summary="定时任务执行历史（最近 N 条）")
def schedule_logs(db: DbSession, taskId: str = "", limit: int = 100) -> dict[str, Any]:
    """翻历史用：每次执行一行，含状态、选中数、成功数、失败数与原因。

    老版把历史追加在 `publish-schedule-log.json` 里，界面上能翻「昨天跑了几轮」。
    新版此前只留最近一次结果，上一轮干了什么永远查不到。
    """
    return schedule_service.list_logs(db, task_id=taskId, limit=limit)


@router.post("/{task_id}/run", summary="立即执行一次（演练）")
def run_schedule(task_id: str, db: DbSession,
                 operator: OperatorDep) -> dict[str, Any]:
    """同步执行一轮：校验店铺 → 取仓库商品 → 发布 → 记账。

    与后台 run_due 共用 schedule_service.execute()，返回同一组字段：
    requested（计划 perRun）/ selected（实际选中）/ published（成功数）/
    failed（实际失败数）/ status（empty | ok | partial | failed）/ reason。

    这条是「人手动触发的一次真发布」，比后台定时那轮更需要在审计里区分开：
    半夜那轮没有「谁」，这一轮有。
    """
    require_writable(db)
    task = schedule_service.get_schedule(db, task_id)
    account = account_service.get_account(db, task.account_id)
    if account is None:
        raise errors.not_found(f"账号 {task.account_id} 已不存在",
                               accountId=str(task.account_id))
    account_service.require_shop(db, account, task.shop_id)

    task_name = str(task.name or "")
    shop_id = str(task.shop_id or "")
    with audit_service.guard(db, operator=operator, action="schedule.run",
                             target_kind="schedule", target_id=str(task_id),
                             shop_id=shop_id, detail={"name": task_name}):
        result = schedule_service.execute(db, task, raise_on_error=True,
                                          with_results=True)
    db.commit()
    audit_service.record(db, operator=operator, action="schedule.run",
                         target_kind="schedule", target_id=str(task_id),
                         shop_id=shop_id,
                         # status 直接进审计：跑了 10 件成功 0 件时，
                         # 审计页要能一眼看出这轮是空的，不用点进去
                         detail={"name": task_name,
                                 "status": result.get("status") or "",
                                 "requested": result.get("requested") or 0,
                                 "selected": result.get("selected") or 0,
                                 "published": result.get("published") or 0,
                                 "failed": result.get("failed") or 0},
                         message=str(result.get("reason") or ""))
    db.commit()
    return result
