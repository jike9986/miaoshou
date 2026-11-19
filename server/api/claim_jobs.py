"""Persistent warehouse-to-collection-box tasks."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query

from api.deps import DbSession, OperatorDep, require_permission, require_writable
from core import errors
from schemas.requests import PublishRequest
from services import account_service, batch_service, claim_job_service

router = APIRouter(prefix="/claim-jobs", tags=["认领任务"],
                   dependencies=[require_permission("publish_claimed")])


@router.post("", status_code=201)
def create_job(payload: PublishRequest, background: BackgroundTasks, db: DbSession,
               operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    if not payload.accountId:
        raise errors.validation("请选择妙手账号", field="accountId")
    account = account_service.require_account(db, payload.accountId)
    account_service.require_shop(db, account, payload.shopId)
    body = payload.model_dump(by_alias=False, exclude_none=True)
    body["confirm"] = payload.confirm is True
    body["claimOnly"] = True
    batch = claim_job_service.create(db, body, account.id, getattr(operator, "id", None))
    db.commit()
    background.add_task(claim_job_service.run, batch.id)
    return {"batch": batch_service.serialize(batch),
            "items": [batch_service.serialize_item(row)
                      for row in batch_service.items_of(db, batch.id)]}


@router.get("")
def list_jobs(db: DbSession, operator: OperatorDep, accountId: str,
              limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    account = account_service.require_account(db, accountId)
    return claim_job_service.list_jobs(db, account.id, limit, operator=operator)


@router.get("/{batch_id}")
def job_detail(batch_id: str, db: DbSession, operator: OperatorDep,
               accountId: str) -> dict[str, Any]:
    account = account_service.require_account(db, accountId)
    batch = claim_job_service.get(db, batch_id, account.id, operator=operator)
    return {"batch": batch_service.serialize(batch),
            "items": [batch_service.serialize_item(row)
                      for row in batch_service.items_of(db, batch.id)]}


@router.delete("/{batch_id}")
def delete_job(batch_id: str, db: DbSession, operator: OperatorDep,
               accountId: str) -> dict[str, Any]:
    """删除任务记录。发布任务与认领任务按 kind 分发到各自的 service。

    只删本地的批次台账，妙手侧已发出的商品不受影响（那是不可撤销动作）。
    """
    require_writable(db)
    account = account_service.require_account(db, accountId)
    batch = claim_job_service.get(db, batch_id, account.id, operator=operator)
    kind = str((batch.settings or {}).get("kind") or "")
    if kind == "publish_job":
        from services import publish_job_service

        result = publish_job_service.delete(db, batch_id, account.id, operator=operator)
    else:
        result = claim_job_service.delete(db, batch_id, account.id, operator=operator)
    db.commit()
    return result
