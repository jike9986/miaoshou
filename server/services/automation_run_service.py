"""Persistent run records for the collect-box automation screen."""
from __future__ import annotations

import secrets
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.utils import now_ts
from models.account import Account
from models.automation_run import AutomationRun, AutomationRunItem
from models.operator import Operator
from services import operator_service

PROCESS_STATUSES = {"queued", "running", "succeeded", "failed", "needs_review"}
PUBLISH_STATUSES = {"not_requested", "queued", "running", "succeeded", "failed", "needs_review"}


def _teacher_or_admin(operator: Operator) -> bool:
    return operator_service.role_of(operator) in {
        operator_service.ROLE_ADMIN, operator_service.ROLE_TEACHER}


def _assert_account_access(account: Account, operator: Operator) -> None:
    if (operator_service.role_of(operator) == operator_service.ROLE_MEMBER
            and account.owner_operator_id != operator.id):
        raise errors.AppError(errors.FORBIDDEN, "无权访问其他学员的自动化运行", status=403)


def _recompute(run: AutomationRun, rows: list[AutomationRunItem]) -> None:
    process = [row.process_status for row in rows]
    publish = [row.publish_status for row in rows]
    if "running" in process or "running" in publish:
        run.status = "running"
        run.finished_at = None
    elif "queued" in process or "queued" in publish:
        run.status = "queued"
        run.finished_at = None
    elif "needs_review" in process or "needs_review" in publish:
        run.status = "needs_review"
        run.finished_at = now_ts()
    elif "failed" in process or "failed" in publish:
        run.status = "partial"
        run.finished_at = now_ts()
    else:
        run.status = "completed"
        run.finished_at = now_ts()
    run.updated_at = now_ts()


def _items(db: Session, run_id: str) -> list[AutomationRunItem]:
    return (db.query(AutomationRunItem).filter(AutomationRunItem.run_id == run_id)
            .order_by(AutomationRunItem.id.asc()).all())


def serialize_item(row: AutomationRunItem) -> dict[str, Any]:
    return {
        "id": str(row.id), "key": row.key, "detailId": row.detail_id,
        "shopId": row.source_shop_id, "title": row.title,
        "processStatus": row.process_status, "processReason": row.process_reason or "",
        "productId": str(row.product_id) if row.product_id else None,
        "processResult": row.process_result if isinstance(row.process_result, dict) else {},
        "publishStatus": row.publish_status, "publishReason": row.publish_reason or "",
        "publishResult": row.publish_result if isinstance(row.publish_result, dict) else {},
        "updatedAt": row.updated_at,
    }


def serialize_run(run: AutomationRun, rows: list[AutomationRunItem]) -> dict[str, Any]:
    return {
        "id": run.id, "ownerOperatorId": str(run.owner_operator_id),
        "accountId": str(run.account_id), "shopId": run.shop_id,
        "status": run.status, "settings": run.settings or {},
        "createdAt": run.created_at, "updatedAt": run.updated_at,
        "finishedAt": run.finished_at, "items": [serialize_item(row) for row in rows],
    }


def create(db: Session, operator: Operator, *, account_id: Any, shop_id: Any,
           settings: Mapping[str, Any], items: Iterable[Mapping[str, Any]]) -> AutomationRun:
    account_text = str(account_id or "").strip()
    if not account_text.isdigit():
        raise errors.validation("accountId 必须是数字", field="accountId")
    account = db.query(Account).filter(Account.id == int(account_text)).one_or_none()
    if account is None:
        raise errors.not_found(f"账号 {account_text} 不存在", accountId=account_text)
    _assert_account_access(account, operator)
    shop_text = str(shop_id or "").strip()
    if not shop_text.isdigit():
        raise errors.validation("shopId 必须是数字", field="shopId")

    clean: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        detail_id = str(item.get("detailId") or "").strip()
        source_shop_id = str(item.get("shopId") or "").strip()
        key = str(item.get("key") or f"{detail_id}:{source_shop_id}").strip()
        if not detail_id.isdigit() or not source_shop_id.isdigit():
            raise errors.validation("商品 detailId 和 shopId 必须是数字", field="items")
        if key in seen:
            continue
        if len(key) > 80:
            raise errors.validation("商品 key 不能超过 80 个字符", field="items")
        seen.add(key)
        clean.append({"key": key, "detail_id": detail_id,
                      "shop_id": source_shop_id,
                      "title": str(item.get("title") or "")[:500]})
        if len(clean) > 200:
            raise errors.validation("一次最多运行 200 件商品", field="items")
    if not clean:
        raise errors.validation("请至少选择一件商品", field="items")

    run = AutomationRun(
        id=f"arun_{secrets.token_hex(12)}", owner_operator_id=operator.id,
        account_id=account.id, shop_id=shop_text, status="queued",
        settings=dict(settings), created_at=now_ts(), updated_at=now_ts(),
    )
    db.add(run)
    db.flush()
    wants_publish = bool(settings.get("publishEnabled"))
    for item in clean:
        db.add(AutomationRunItem(
            run_id=run.id, key=item["key"], detail_id=item["detail_id"],
            source_shop_id=item["shop_id"], title=item["title"],
            process_status="queued", process_result={},
            publish_status="queued" if wants_publish else "not_requested",
            publish_result={}, created_at=now_ts(), updated_at=now_ts(),
        ))
    db.flush()
    return run


def get(db: Session, run_id: str, operator: Operator) -> AutomationRun:
    run = db.query(AutomationRun).filter(AutomationRun.id == str(run_id)).one_or_none()
    if run is None:
        raise errors.not_found(f"自动化运行 {run_id} 不存在", runId=str(run_id))
    if not _teacher_or_admin(operator) and run.owner_operator_id != operator.id:
        raise errors.AppError(errors.FORBIDDEN, "无权访问其他学员的自动化运行", status=403)
    return run


def latest(db: Session, operator: Operator, *, account_id: Any,
           shop_id: Any = None) -> AutomationRun | None:
    text = str(account_id or "").strip()
    if not text.isdigit():
        raise errors.validation("accountId 必须是数字", field="accountId")
    account = db.query(Account).filter(Account.id == int(text)).one_or_none()
    if account is None:
        raise errors.not_found(f"账号 {text} 不存在", accountId=text)
    _assert_account_access(account, operator)
    query = db.query(AutomationRun).filter(AutomationRun.account_id == account.id)
    if shop_id not in (None, ""):
        query = query.filter(AutomationRun.shop_id == str(shop_id))
    if not _teacher_or_admin(operator):
        query = query.filter(AutomationRun.owner_operator_id == operator.id)
    return query.order_by(AutomationRun.created_at.desc(), AutomationRun.id.desc()).first()


def get_full(db: Session, run: AutomationRun) -> dict[str, Any]:
    return serialize_run(run, _items(db, run.id))


def recover(db: Session, run: AutomationRun) -> dict[str, Any]:
    for row in _items(db, run.id):
        if row.process_status == "running":
            row.process_status = "needs_review"
            row.process_reason = "客户端在处理响应前中断；请核对妙手与商品记录后再处理。"
            row.updated_at = now_ts()
        if row.publish_status == "running":
            row.publish_status = "needs_review"
            row.publish_reason = "客户端在发布响应前中断；请先核对妙手发布记录。"
            row.updated_at = now_ts()
        elif row.process_status == "succeeded" and row.publish_status == "queued":
            row.publish_status = "needs_review"
            row.publish_reason = "商品已处理但发布未完成；请核对图片与商品信息后再发布。"
            row.updated_at = now_ts()
    _recompute(run, _items(db, run.id))
    db.commit()
    return get_full(db, run)


def start_item(db: Session, run: AutomationRun, item_id: Any) -> dict[str, Any]:
    row = db.query(AutomationRunItem).filter(
        AutomationRunItem.run_id == run.id,
        AutomationRunItem.id == int(item_id),
    ).one_or_none()
    if row is None:
        raise errors.not_found("自动化运行商品不存在", itemId=str(item_id))
    changed = (db.query(AutomationRunItem).filter(
        AutomationRunItem.id == row.id,
        AutomationRunItem.process_status == "queued",
    ).update({"process_status": "running", "process_reason": "",
              "updated_at": now_ts()}, synchronize_session="fetch"))
    if changed != 1:
        raise errors.AppError("CONFLICT", "该商品已开始或已有处理结果，不会重复启动",
                              status=409)
    run.status = "running"
    run.finished_at = None
    run.updated_at = now_ts()
    db.commit()
    return get_full(db, run)


def finish_process(db: Session, run: AutomationRun, item_id: Any, *, status: str,
                   reason: str = "", product_id: Any = None,
                   result: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if status not in {"succeeded", "failed", "needs_review"}:
        raise errors.validation("处理结果状态无效", field="status")
    row = _get_item(db, run, item_id)
    if row.process_status not in {"running", "needs_review"}:
        raise errors.AppError("CONFLICT", "该商品不处于可记录处理结果的状态",
                              status=409)
    product_pk = None
    if product_id not in (None, ""):
        text = str(product_id)
        if not text.isdigit():
            raise errors.validation("productId 必须是数字", field="productId")
        product_pk = int(text)
    row.process_status = status
    row.process_reason = str(reason or "")[:2000]
    row.product_id = product_pk
    row.process_result = dict(result or {})
    if row.publish_status == "queued" and status in {"failed", "needs_review"}:
        row.publish_status = status
        row.publish_reason = (
            "商品处理失败，未进入发布。" if status == "failed"
            else "商品处理状态待核对，暂不发布。"
        )
    row.updated_at = now_ts()
    _recompute(run, _items(db, run.id))
    db.commit()
    return get_full(db, run)


def start_publish(db: Session, run: AutomationRun, item_id: Any) -> dict[str, Any]:
    row = _get_item(db, run, item_id)
    if row.process_status != "succeeded" or row.product_id is None:
        raise errors.AppError("CONFLICT", "商品处理完成后才能发布", status=409)
    changed = (db.query(AutomationRunItem).filter(
        AutomationRunItem.id == row.id,
        AutomationRunItem.publish_status == "queued",
    ).update({"publish_status": "running", "publish_reason": "",
              "updated_at": now_ts()}, synchronize_session="fetch"))
    if changed != 1:
        raise errors.AppError("CONFLICT", "该商品发布已开始或已有结果，不会重复启动",
                              status=409)
    run.status = "running"
    run.finished_at = None
    run.updated_at = now_ts()
    db.commit()
    return get_full(db, run)


def finish_publish(db: Session, run: AutomationRun, item_id: Any, *, status: str,
                    reason: str = "", result: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if status not in {"succeeded", "failed", "needs_review"}:
        raise errors.validation("发布结果状态无效", field="status")
    row = _get_item(db, run, item_id)
    if row.publish_status not in {"running", "needs_review"} and not (
        row.publish_status == "queued" and status == "needs_review"
    ):
        raise errors.AppError("CONFLICT", "该商品不处于可记录发布结果的状态",
                              status=409)
    row.publish_status = status
    row.publish_reason = str(reason or "")[:2000]
    row.publish_result = dict(result or {})
    row.updated_at = now_ts()
    _recompute(run, _items(db, run.id))
    db.commit()
    return get_full(db, run)


def _get_item(db: Session, run: AutomationRun, item_id: Any) -> AutomationRunItem:
    try:
        ident = int(str(item_id))
    except (TypeError, ValueError):
        raise errors.not_found("自动化运行商品不存在", itemId=str(item_id)) from None
    row = db.query(AutomationRunItem).filter(
        AutomationRunItem.run_id == run.id, AutomationRunItem.id == ident,
    ).one_or_none()
    if row is None:
        raise errors.not_found("自动化运行商品不存在", itemId=str(item_id))
    return row
