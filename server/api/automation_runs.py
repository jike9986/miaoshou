"""Durable state for interactive collect-box automation runs."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import Field, StrictInt

from api.deps import DbSession, OperatorDep, require_permission, require_writable
from schemas.common import CamelModel
from services import automation_run_service

router = APIRouter(prefix="/automation-runs", tags=["自动化运行"])


class RunItemInput(CamelModel):
    key: str = ""
    detailId: str
    shopId: str
    title: str = ""


class RunCreateRequest(CamelModel):
    accountId: str
    shopId: str
    settings: dict[str, Any] = Field(default_factory=dict)
    items: list[RunItemInput] = Field(min_length=1, max_length=200)


class ProcessResultRequest(CamelModel):
    status: Literal["succeeded", "failed", "needs_review"]
    reason: str = ""
    productId: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class PublishResultRequest(CamelModel):
    status: Literal["succeeded", "failed", "needs_review"]
    reason: str = ""
    result: dict[str, Any] = Field(default_factory=dict)


class PublishStartRequest(CamelModel):
    confirmed_product_id: str | None = None
    confirmed_revision: StrictInt | None = None


@router.post("", status_code=201, dependencies=[require_permission("process")])
def create_run(payload: RunCreateRequest, db: DbSession,
               operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    items = [item.model_dump(by_alias=False) for item in payload.items]
    run = automation_run_service.create(
        db, operator, account_id=payload.accountId, shop_id=payload.shopId,
        settings=payload.settings, items=items,
    )
    db.commit()
    return {"run": automation_run_service.get_full(db, run)}


@router.get("/latest")
def latest_run(db: DbSession, operator: OperatorDep, accountId: str,
               shopId: str | None = None) -> dict[str, Any]:
    run = automation_run_service.latest(db, operator, account_id=accountId,
                                        shop_id=shopId)
    return {"run": automation_run_service.get_full(db, run) if run else None}


@router.get("/{run_id}")
def get_run(run_id: str, db: DbSession, operator: OperatorDep) -> dict[str, Any]:
    run = automation_run_service.get(db, run_id, operator)
    return {"run": automation_run_service.get_full(db, run)}


@router.post("/{run_id}/recover", dependencies=[require_permission("process")])
def recover_run(run_id: str, db: DbSession, operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    run = automation_run_service.get(db, run_id, operator)
    return {"run": automation_run_service.recover(db, run)}


@router.post("/{run_id}/items/{item_id}/start",
             dependencies=[require_permission("process")])
def start_item(run_id: str, item_id: str, db: DbSession,
               operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    run = automation_run_service.get(db, run_id, operator)
    return {"run": automation_run_service.start_item(db, run, item_id)}


@router.post("/{run_id}/items/{item_id}/process-result",
             dependencies=[require_permission("process")])
def process_result(run_id: str, item_id: str, payload: ProcessResultRequest,
                   db: DbSession, operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    run = automation_run_service.get(db, run_id, operator)
    return {"run": automation_run_service.finish_process(
        db, run, item_id, status=payload.status, reason=payload.reason,
        product_id=payload.productId, result=payload.result)}


@router.post("/{run_id}/items/{item_id}/publish-start",
             dependencies=[require_permission("publish")])
def start_publish(run_id: str, item_id: str, db: DbSession,
                  operator: OperatorDep,
                  payload: PublishStartRequest = PublishStartRequest()) -> dict[str, Any]:
    require_writable(db)
    run = automation_run_service.get(db, run_id, operator)
    return {"run": automation_run_service.start_publish(
        db, run, item_id,
        confirmed_product_id=payload.confirmed_product_id,
        confirmed_revision=payload.confirmed_revision)}


@router.post("/{run_id}/items/{item_id}/resume-publish",
             dependencies=[require_permission("publish")])
def resume_publish(run_id: str, item_id: str, db: DbSession,
                   operator: OperatorDep,
                   payload: PublishStartRequest = PublishStartRequest()) -> dict[str, Any]:
    require_writable(db)
    run = automation_run_service.get(db, run_id, operator)
    return {"run": automation_run_service.resume_publish_after_images(
        db, run, item_id,
        confirmed_product_id=payload.confirmed_product_id,
        confirmed_revision=payload.confirmed_revision)}


@router.post("/{run_id}/items/{item_id}/retry-publish",
             dependencies=[require_permission("publish")])
def retry_publish(run_id: str, item_id: str, db: DbSession,
                  operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    run = automation_run_service.get(db, run_id, operator)
    return {"run": automation_run_service.retry_publish(db, run, item_id)}


@router.post("/{run_id}/items/{item_id}/publish-result",
             dependencies=[require_permission("publish")])
def publish_result(run_id: str, item_id: str, payload: PublishResultRequest,
                   db: DbSession, operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    run = automation_run_service.get(db, run_id, operator)
    return {"run": automation_run_service.finish_publish(
        db, run, item_id, status=payload.status, reason=payload.reason,
        result=payload.result)}
