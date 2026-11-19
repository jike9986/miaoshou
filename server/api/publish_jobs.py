"""发布到店铺的后台任务（一次可发多个店铺）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query

from api.deps import DbSession, OperatorDep, require_permission, require_writable
from core import errors
from services import account_service, batch_service, publish_job_service

router = APIRouter(prefix="/publish-jobs", tags=["发布任务"],
                   dependencies=[require_permission("publish_claimed")])


@router.post("", status_code=201)
def create_job(payload: dict[str, Any], background: BackgroundTasks, db: DbSession,
               operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    account_id = payload.get("accountId")
    if not account_id:
        raise errors.validation("请选择妙手账号", field="accountId")
    account = account_service.require_account(db, str(account_id))
    batch = publish_job_service.create(db, payload, account.id,
                                       getattr(operator, "id", None))
    db.commit()
    background.add_task(publish_job_service.run, batch.id)
    return {"batch": batch_service.serialize(batch),
            "items": [batch_service.serialize_item(row)
                      for row in batch_service.items_of(db, batch.id)]}


@router.get("")
def list_jobs(db: DbSession, operator: OperatorDep, accountId: str,
              limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    account = account_service.require_account(db, accountId)
    return publish_job_service.list_jobs(db, account.id, limit, operator=operator)


@router.get("/{batch_id}")
def job_detail(batch_id: str, db: DbSession, operator: OperatorDep,
               accountId: str) -> dict[str, Any]:
    account = account_service.require_account(db, accountId)
    batch = publish_job_service.get(db, batch_id, account.id, operator=operator)
    return {"batch": batch_service.serialize(batch),
            "items": [batch_service.serialize_item(row)
                      for row in batch_service.items_of(db, batch.id)]}


@router.delete("/{batch_id}")
def delete_job(batch_id: str, db: DbSession, operator: OperatorDep,
               accountId: str) -> dict[str, Any]:
    require_writable(db)
    account = account_service.require_account(db, accountId)
    result = publish_job_service.delete(db, batch_id, account.id, operator=operator)
    db.commit()
    return result
