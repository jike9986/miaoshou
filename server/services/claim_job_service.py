"""Persist each claim before calling the upstream service."""
from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.locks import hold_for_session
from core.utils import new_batch_id, now_ts
from db.session import SessionLocal
from models.automation_run import AutomationRun, AutomationRunItem
from models.batch import Batch, BatchItem
from models.operator import Operator
from models.product import Product
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
    hold_for_session(db, [f"claim-create:{account_id}:{operator_id}"], blocking=False,
                     busy_message="认领任务正在创建，请稍后查看批次")
    active = db.query(Batch).filter(
        Batch.account_id == account_id, Batch.operator_id == operator_id,
        Batch.shop_id == str(body["shopId"]),
        Batch.status.in_(["pending", "running"])).all()
    for previous in active:
        if ((previous.settings or {}).get("kind") == KIND
                and set(ids).intersection(str(value) for value in previous.detail_ids or [])):
            raise errors.busy("这些商品已有认领任务，请在批次中查看进度", batchId=previous.id)
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
    """Members may import shared finished products or their own automation results."""
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
                if (product.id in owned_automation_results
                        and product.account_id is not None
                        and account_service.has_account_access(
                            db, product.account_id, operator.id)):
                    continue
                raise errors.AppError(
                    errors.FORBIDDEN,
                    "无权访问其他学员的自动化处理结果", status=403,
                    details={"warehouseId": ident},
                )


def _is_global_operator(operator: Operator | None) -> bool:
    return (operator is None
            or operator_service.role_of(operator) in {
                operator_service.ROLE_ADMIN, operator_service.ROLE_TEACHER})


def _assert_job_owner(batch: Batch, operator: Operator | None) -> None:
    if not _is_global_operator(operator) and batch.operator_id != operator.id:
        raise errors.not_found("任务不存在", batchId=batch.id)


def _claim_chunk_outcome(result: Any, chunk: list[dict[str, Any]]) -> tuple[
        set[str], dict[str, str], bool]:
    """Resolve one completed publish response without guessing on unknown results."""
    if not isinstance(result, dict):
        return set(), {}, False
    claimed = result.get("claimedWarehouseIds")
    if not isinstance(claimed, list):
        return set(), {}, False

    expected = [str(item.get("warehouseId") or "").strip() for item in chunk]
    expected_set = set(expected)
    succeeded = {str(value) for value in claimed if str(value) in expected_set}
    if (len(succeeded) != len(claimed)
            or any(str(value) not in expected_set for value in claimed)):
        return succeeded, {}, False

    missing = expected_set - succeeded
    messages: dict[str, str] = {}
    failures = result.get("failures")
    if isinstance(failures, str):
        failures = [failures] if failures else []
    if not isinstance(failures, list):
        return succeeded, messages, not missing and not failures
    if not missing:
        return succeeded, messages, not failures
    if not failures:
        return succeeded, messages, False

    has_failure_identity = False
    has_unmapped_failure = False
    for failure in failures:
        if isinstance(failure, dict):
            reason = str(failure.get("reason") or failure.get("message")
                         or failure.get("error") or "").strip()
            stage = str(failure.get("stage") or "").strip().lower()
            warehouse_id = str(failure.get("warehouseId") or "").strip()
            source_index = failure.get("sourceIndex")
            if source_index is not None:
                if isinstance(source_index, bool) or not isinstance(source_index, int):
                    return succeeded, messages, False
                try:
                    index = int(source_index)
                except (TypeError, ValueError):
                    return succeeded, messages, False
                if index < 0 or index >= len(expected):
                    return succeeded, messages, False
                indexed_id = expected[index]
                if warehouse_id and warehouse_id != indexed_id:
                    return succeeded, messages, False
                warehouse_id = warehouse_id or indexed_id
            has_failure_identity = (
                has_failure_identity or bool(warehouse_id) or source_index is not None)
            if (stage in {"collect_box_verify", "claim_result_unknown", "upstream_unknown"}
                    or any(marker in reason for marker in (
                        "结果未知", "尚未确认", "勿重复操作", "先检查采集箱"))):
                return succeeded, messages, False
            if not reason:
                return succeeded, messages, False
            if warehouse_id in succeeded:
                return succeeded, messages, False
            if warehouse_id:
                if warehouse_id not in missing:
                    return succeeded, messages, False
                if warehouse_id in messages and messages[warehouse_id] != reason:
                    return succeeded, messages, False
                messages[warehouse_id] = reason[:500]
            else:
                has_unmapped_failure = True
            if failure.get("certain") is False:
                if warehouse_id in messages:
                    messages[warehouse_id] = (
                        f"结果未知，请先核对采集箱：{messages[warehouse_id]}")[:500]
                return succeeded, messages, False
        elif not str(failure or "").strip():
            return succeeded, messages, False
        else:
            has_unmapped_failure = True

    if missing == messages.keys():
        return succeeded, messages, not has_unmapped_failure

    if not has_failure_identity and len(missing) == 1 and len(failures) == 1:
        # Older responses omitted per-item identity. A single missing product
        # and a single explicit failure still have an unambiguous mapping.
        legacy_reasons = [
            str(value.get("reason") or value.get("message") or value.get("error") or "").strip()
            if isinstance(value, dict) else str(value or "").strip()
            for value in failures
        ]
        legacy_reasons = [reason for reason in legacy_reasons if reason]
        if legacy_reasons:
            return succeeded, {next(iter(missing)): legacy_reasons[0][:500]}, True
    return succeeded, messages, False


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
        if (batch.settings or {}).get("kind") != KIND:
            return
        # Repeated worker delivery must not copy the same batch twice.
        started = db.query(Batch).filter(
            Batch.id == batch_id, Batch.status == "pending").update(
                {Batch.status: "running"}, synchronize_session=False)
        db.commit()
        if started != 1:
            return
        db.refresh(batch)
        body = dict((batch.settings or {}).get("request") or {})
        source_items = body.get("items") or []
        item_ids = [item.id for item in batch_service.items_of(db, batch_id)]
        for start in range(0, len(source_items), COPY_BATCH_SIZE):
            chunk = source_items[start:start + COPY_BATCH_SIZE]
            chunk_ids = item_ids[start:start + len(chunk)]
            for item_id in chunk_ids:
                item = db.get(BatchItem, item_id)
                item.status = "running"
                item.attempt += 1
            progress = {"stage": "copying", "offset": start,
                        "total": len(source_items), "groupSize": len(chunk),
                        "processed": 0, "verified": 0, "failed": 0}
            batch.settings = {**(batch.settings or {}), "progress": progress}
            batch.updated_at = now_ts()
            db.commit()
            body["items"] = chunk
            body["accountId"] = str(batch.account_id)
            body["batchId"] = batch.id
            body["forceRebuild"] = True
            body["claimOnly"] = True

            def report(event: dict[str, Any], _progress: dict[str, Any] = progress) -> None:
                # Commit progress separately without releasing publish locks.
                # SQLite allows one writer, so retain its group boundaries.
                if db.get_bind().dialect.name == "sqlite":
                    return
                _progress.update({key: value for key, value in event.items()
                                  if key in {"stage", "processed", "verified", "saved", "failed",
                                             "warehouseId"}})
                progress_db = SessionLocal()
                try:
                    current = progress_db.get(Batch, batch_id)
                    if current is not None and current.status == "running":
                        current.settings = {**(current.settings or {}),
                                            "progress": dict(_progress)}
                        current.updated_at = now_ts()
                        progress_db.commit()
                except Exception:
                    progress_db.rollback()
                    logger.warning("Could not persist claim progress for %s", batch_id,
                                   exc_info=True)
                finally:
                    progress_db.close()
            stop_after_chunk = False
            unknown_message = "处理失败或结果未知，请先在采集箱核对"
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
                    db, body, _log_operator_id=batch.operator_id, _claim_progress=report)
                succeeded, failure_messages, known_outcome = _claim_chunk_outcome(
                    result, chunk)
                if not known_outcome:
                    stop_after_chunk = True
                    logger.warning(
                        "Claim job %s batch at %s returned an unknown outcome",
                        batch_id, start)
                db.commit()
            except Exception as exc:  # upstream outcome can be unknown; never retry automatically
                db.rollback()
                succeeded = set()
                failure_messages = {}
                unknown_message = f"处理失败或结果未知，请先在采集箱核对：{exc}"[:500]
                stop_after_chunk = True
                logger.warning("Claim job %s batch at %s failed", batch_id, start, exc_info=True)
            db.expire_all()
            batch = batch_service.get(db, batch_id)
            rows = list(batch.results or [])
            for item_id in chunk_ids:
                item = db.get(BatchItem, item_id)
                item.status = "ok" if item.detail_id in succeeded else "failed"
                if item.status == "ok":
                    item.message = "认领成功"
                else:
                    item.message = (failure_messages.get(item.detail_id)
                                    or (unknown_message if stop_after_chunk
                                        else "认领失败，旧版回执未提供单件原因"))
                rows.append({"detailId": item.detail_id, "status": item.status,
                             "message": item.message, "at": now_ts()})
            batch.current_index = start + len(chunk)
            batch.results = rows
            batch.updated_at = now_ts()
            db.commit()
            if stop_after_chunk:
                break
        batch.status = (
            "failed"
            if any(row.status == "failed" for row in batch_service.items_of(db, batch_id))
            else "done"
        )
        batch.finished_at = now_ts()
        batch.settings = {**(batch.settings or {}), "progress": {
            **(batch.settings or {}).get("progress", {}), "stage": batch.status}}
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Claim job %s stopped unexpectedly", batch_id)
        # Preserve confirmed results and pending work. Unknown upstream writes
        # require manual reconciliation rather than an automatic duplicate copy.
        try:
            batch = batch_service.get(db, batch_id)
            if batch.status == "running":
                rows = list(batch.results or [])
                for item in batch_service.items_of(db, batch_id):
                    if item.status == "running":
                        item.status = "failed"
                        item.message = "任务中断，入箱结果未知，请先到采集箱核对"
                        rows.append({"detailId": item.detail_id, "status": "failed",
                                     "message": item.message, "at": now_ts()})
                batch.results = rows
                batch.current_index = len(rows)
                batch.status = "failed"
                batch.errors = [*(batch.errors or []), "任务中断，未处理商品仍为待处理"]
                batch.finished_at = now_ts()
                batch.updated_at = now_ts()
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("Could not finalize interrupted claim job %s", batch_id)
    finally:
        db.close()
