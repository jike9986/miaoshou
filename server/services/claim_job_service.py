"""Persist each claim before calling the upstream service."""
from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.utils import new_batch_id, now_ts
from db.session import SessionLocal
from models.batch import Batch, BatchItem
from services import batch_service, publish_service

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
    batch = Batch(id=new_batch_id("clm"), status="pending", account_id=account_id,
                  shop_id=str(body["shopId"]), detail_ids=ids, current_index=0,
                  settings={"kind": KIND, "request": body}, results=[], errors=[],
                  operator_id=operator_id)
    db.add(batch)
    db.flush()
    db.add_all([BatchItem(batch_id=batch.id, detail_id=value, status="pending") for value in ids])
    db.flush()
    return batch


def get(db: Session, batch_id: str, account_id: int) -> Batch:
    batch = batch_service.get(db, batch_id)
    if batch.account_id != account_id or (batch.settings or {}).get("kind") != KIND:
        raise errors.not_found("任务不存在", batchId=batch_id)
    return batch


def list_jobs(db: Session, account_id: int, limit: int) -> dict[str, Any]:
    # Filtering the JSON in Python keeps SQLite and PostgreSQL behaviour consistent.
    rows = (db.query(Batch).filter(Batch.account_id == account_id)
            .order_by(Batch.created_at.desc()).all())
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
                result = publish_service.publish(db, body, operator_id=batch.operator_id)
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
        batch.status = "failed" if any(row.status == "failed" for row in batch_service.items_of(db, batch_id)) else "done"
        batch.finished_at = now_ts()
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Claim job %s stopped unexpectedly", batch_id)
    finally:
        db.close()
