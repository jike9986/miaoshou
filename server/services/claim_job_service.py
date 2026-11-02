"""Persist each claim before calling the upstream service."""
from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.utils import new_batch_id, now_ts
from db.session import SessionLocal
from models.automation_run import AutomationRun, AutomationRunItem
from models.batch import Batch, BatchItem
from models.operator import Operator
from models.product import Product
from models.product_claim import ProductClaim
from services import (
    account_service,
    batch_service,
    operator_service,
    publish_service,
)

logger = logging.getLogger(__name__)
KIND = "warehouse_claim"
MAX_ITEMS = 5000
COPY_BATCH_SIZE = 100


def create(db: Session, body: dict[str, Any], account_id: int, operator_id: int | None) -> Batch:
    if body.get("confirm") is not True:
        raise errors.validation("请确认认领", field="confirm")
    if not str(body.get("shopId") or "").strip():
        raise errors.validation("请选择目标店铺", field="shopId")
    items = body.get("items") or []
    ids = [str(item.get("warehouseId") or "").strip() for item in items]
    if not ids or any(not value.isdigit() for value in ids):
        raise errors.validation("请选择仓库商品", field="items")
    if len(ids) > MAX_ITEMS or len(set(ids)) != len(ids):
        raise errors.validation("商品重复或数量超过 5000 件", field="items")
    operator = operator_service.get(db, operator_id) if operator_id is not None else None
    _validate_member_items(db, ids, operator, account_id)
    batch = Batch(id=new_batch_id("clm"), status="pending", account_id=account_id,
                  shop_id=str(body["shopId"]), detail_ids=ids, current_index=0,
                  settings={"kind": KIND, "request": body}, results=[], errors=[],
                  operator_id=operator_id)
    db.add(batch)
    db.flush()
    db.add_all([BatchItem(batch_id=batch.id, detail_id=value, status="pending") for value in ids])
    db.flush()
    return batch


def _validate_member_items(db: Session, warehouse_ids: list[str],
                           operator: Operator | None, account_id: int) -> None:
    """Members may copy only products they claimed or automation results they own."""
    if operator is None or operator_service.role_of(operator) != operator_service.ROLE_MEMBER:
        return
    if not account_service.has_account_access(db, account_id, operator.id):
        raise errors.AppError(errors.FORBIDDEN,
                              "无权访问该妙手账号", status=403)
    from services.warehouse_service import active_query

    # Keep each query below SQLite's bind-parameter limit; large claim jobs may
    # contain thousands of items, while execution rechecks only 100 at a time.
    for start in range(0, len(warehouse_ids), COPY_BATCH_SIZE):
        id_chunk = warehouse_ids[start:start + COPY_BATCH_SIZE]
        product_ids = [int(ident) for ident in id_chunk]
        products = {
            str(product.id): product
            for product in active_query(db).filter(Product.id.in_(product_ids)).all()
        }
        missing = next((ident for ident in id_chunk if ident not in products), None)
        if missing is not None:
            raise errors.not_found(f"仓库里没有商品 {missing}", id=missing)

        claims = {
            int(row[0]) for row in db.query(ProductClaim.product_id).filter(
                ProductClaim.product_id.in_(product_ids),
                ProductClaim.owner_operator_id == operator.id,
                ProductClaim.status == "claimed",
            ).all()
        }
        owned_automation_results = {
            int(row[0]) for row in db.query(AutomationRunItem.product_id)
            .join(AutomationRun, AutomationRun.id == AutomationRunItem.run_id)
            .filter(AutomationRunItem.product_id.in_(product_ids),
                    AutomationRun.owner_operator_id == operator.id)
            .all()
        }
        for ident in id_chunk:
            product = products[ident]
            if product.automation_result:
                if product.id in owned_automation_results:
                    continue
                raise errors.AppError(
                    errors.FORBIDDEN,
                    "无权访问其他学员的自动化处理结果", status=403,
                    details={"warehouseId": ident},
                )
            if product.id not in claims:
                raise errors.AppError(
                    errors.FORBIDDEN,
                    "只能将自己已认领的成品或有权访问的自动化结果导入采集箱",
                    status=403,
                    details={"warehouseId": ident},
                )


def _is_global_operator(operator: Operator | None) -> bool:
    return (operator is None
            or operator_service.role_of(operator) in {
                operator_service.ROLE_ADMIN, operator_service.ROLE_TEACHER})


def _assert_job_owner(batch: Batch, operator: Operator | None) -> None:
    if not _is_global_operator(operator) and batch.operator_id != operator.id:
        raise errors.not_found("任务不存在", batchId=batch.id)


def get(db: Session, batch_id: str, account_id: int,
        operator: Operator | None = None) -> Batch:
    batch = batch_service.get(db, batch_id)
    if batch.account_id != account_id or (batch.settings or {}).get("kind") != KIND:
        raise errors.not_found("任务不存在", batchId=batch_id)
    _assert_job_owner(batch, operator)
    return batch


def list_jobs(db: Session, account_id: int, limit: int,
              operator: Operator | None = None) -> dict[str, Any]:
    # Filtering the JSON in Python keeps SQLite and PostgreSQL behaviour consistent.
    query = db.query(Batch).filter(Batch.account_id == account_id)
    if not _is_global_operator(operator):
        query = query.filter(Batch.operator_id == operator.id)
    rows = query.order_by(Batch.created_at.desc()).all()
    jobs = [row for row in rows if (row.settings or {}).get("kind") == KIND]
    return {"items": [batch_service.serialize(row) for row in jobs[:limit]], "total": len(jobs)}


def recover_interrupted_on_startup() -> int:
    """Enable only when this service is the sole worker using the database."""
    if os.getenv("AOHUANG_CLAIM_RECOVER_SINGLE_WORKER") != "1":
        return 0
    count = 0
    db = SessionLocal()
    try:
        for batch in db.query(Batch).filter(Batch.status.in_(["pending", "running"])).all():
            if (batch.settings or {}).get("kind") != KIND:
                continue
            for item in batch_service.items_of(db, batch.id):
                if item.status != "running":
                    continue
                item.status = "failed"
                item.message = "服务中断，认领结果未知，请先到采集箱核对"
                batch.results = [*(batch.results or []), {"detailId": item.detail_id,
                                 "status": "failed", "message": item.message, "at": now_ts()}]
                batch.current_index = int(batch.current_index or 0) + 1
            batch.status = "failed"
            batch.errors = [*(batch.errors or []), "服务中断，未处理商品仍为待处理"]
            batch.finished_at = now_ts()
            batch.updated_at = now_ts()
            count += 1
        db.commit()
        return count
    finally:
        db.close()


def run(batch_id: str) -> None:
    db = SessionLocal()
    try:
        batch = batch_service.get(db, batch_id)
        body = dict((batch.settings or {}).get("request") or {})
        source_items = body.get("items") or []
        item_ids = [item.id for item in batch_service.items_of(db, batch_id)]
        batch.status = "running"
        db.commit()
        for start in range(0, len(source_items), COPY_BATCH_SIZE):
            chunk = source_items[start:start + COPY_BATCH_SIZE]
            chunk_ids = item_ids[start:start + len(chunk)]
            for item_id in chunk_ids:
                item = db.get(BatchItem, item_id)
                item.status = "running"
                item.attempt += 1
            db.commit()
            body["items"] = chunk
            body["accountId"] = str(batch.account_id)
            body["batchId"] = batch.id
            body["forceRebuild"] = True
            try:
                # Recheck live membership/claims immediately before the upstream
                # side effect; ownership or product claims may change while a job waits.
                owner = (operator_service.get(db, batch.operator_id)
                         if batch.operator_id is not None else None)
                if batch.operator_id is not None and owner is None:
                    raise errors.AppError(errors.FORBIDDEN,
                                          "任务所属账号已失效", status=403)
                if owner is not None:
                    operator_service.require_enabled(owner)
                _validate_member_items(
                    db,
                    [str(raw.get("warehouseId") or "").strip() for raw in chunk],
                    owner,
                    batch.account_id,
                )
                # This is an internal, persisted claim job. Run through the
                # non-student publish branch so claimOnly stays unavailable to
                # direct student publish requests, while still attributing the
                # resulting ledger rows to the job owner.
                result = publish_service.publish(
                    db, body, _log_operator_id=batch.operator_id)
                failures = result.get("failures") or []
                expected = {str(raw["warehouseId"]) for raw in chunk}
                succeeded = set(result.get("claimedWarehouseIds") or []) & expected
                message = str(failures or "未收到认领成功回执")[:500]
                db.commit()
            except Exception as exc:  # upstream outcome can be unknown; never retry automatically
                db.rollback()
                succeeded = set()
                message = f"处理失败或结果未知，请先在采集箱核对：{exc}"[:500]
                logger.warning("Claim job %s batch at %s failed", batch_id, start, exc_info=True)
            batch = batch_service.get(db, batch_id)
            rows = list(batch.results or [])
            for item_id in chunk_ids:
                item = db.get(BatchItem, item_id)
                item.status = "ok" if item.detail_id in succeeded else "failed"
                item.message = "认领成功" if item.status == "ok" else message
                rows.append({"detailId": item.detail_id, "status": item.status,
                             "message": item.message, "at": now_ts()})
            batch.current_index = start + len(chunk)
            batch.results = rows
            batch.updated_at = now_ts()
            db.commit()
            if len(succeeded) != len(chunk):
                break
        batch.status = (
            "failed"
            if any(row.status == "failed" for row in batch_service.items_of(db, batch_id))
            else "done"
        )
        batch.finished_at = now_ts()
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Claim job %s stopped unexpectedly", batch_id)
    finally:
        db.close()
