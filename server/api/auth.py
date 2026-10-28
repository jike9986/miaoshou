"""认证路由：连接 / 状态 / 断开 / 账号管理。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from api.deps import DbSession, OperatorDep, optional_account, require_writable
from core.config import get_settings
from core.constants import (
    APP_VERSION,
    COLLECT_BOX_MAX_ITEMS,
    DETAIL_IMAGE_HARD_LIMIT,
    DETAIL_IMAGE_LIMIT,
    IMAGE_CONCURRENCY,
    MAIN_IMAGE_LIMIT,
    SERVER_NAME,
    SUPPORTED_LANGUAGES,
)
from schemas.requests import ConnectRequest
from services import account_service, audit_service, setting_service

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/connect", summary="连接妙手账号")
def connect(payload: ConnectRequest, db: DbSession,
            operator: OperatorDep) -> dict[str, Any]:
    """连接/切换妙手账号。

    这条要留痕：账号是**全员共用**的那一个，「下午谁把账号换成了另一个店的」
    会让所有人的操作落到别的店铺去。审计只记账号名与 id，**不记 AppSecret**。
    """
    require_writable(db)
    with audit_service.guard(db, operator=operator, action="auth.connect",
                             target_kind="account",
                             target_id=str(payload.accountId or "")):
        result = account_service.connect(
            db,
            app_id=payload.appId,
            app_secret=payload.appSecret,
            account_id=payload.accountId,
            name=payload.name,
            save=payload.save,
            auto_scan=payload.autoScan,
        )
    db.commit()
    account = result.get("account") or {}
    audit_service.record(db, operator=operator, action="auth.connect",
                         target_kind="account",
                         target_id=str(account.get("id") or payload.accountId or ""),
                         detail={"name": str(account.get("name") or payload.name or "")[:64],
                                 "autoScan": bool(payload.autoScan),
                                 "saved": bool(payload.save)})
    db.commit()
    return result


@router.get("/status", summary="当前连接状态")
def status(db: DbSession) -> dict[str, Any]:
    account = optional_account(db)
    settings = get_settings()
    return {
        "connected": account is not None,
        "accountId": str(account.id) if account else None,
        "connectionName": account.name if account else None,
        "appIdHint": account.app_id_hint if account else None,
        "readOnly": setting_service.is_read_only(db),
        "publishEnabled": setting_service.publish_enabled(db),
        "accounts": account_service.account_summaries(db),
        "languages": list(SUPPORTED_LANGUAGES),
        "serverVersion": APP_VERSION,
        "serverName": SERVER_NAME,
        "dataDir": str(settings.data_dir),
        "limits": {
            "mainLimit": MAIN_IMAGE_LIMIT,
            "detailLimit": DETAIL_IMAGE_LIMIT,
            "detailHardLimit": DETAIL_IMAGE_HARD_LIMIT,
            "concurrency": IMAGE_CONCURRENCY,
            "collectBoxMaxItems": COLLECT_BOX_MAX_ITEMS,
        },
    }


@router.delete("/disconnect", summary="断开当前连接")
def disconnect(
    db: DbSession,
    operator: OperatorDep,
    accountId: str | None = None,
    deleteAccount: bool = False,
) -> dict[str, Any]:
    require_writable(db)
    if deleteAccount and accountId:
        with audit_service.guard(db, operator=operator, action="auth.disconnect",
                                 target_kind="account", target_id=str(accountId)):
            result = account_service.delete_account(db, accountId)
        action_detail = {"deleted": True}
    else:
        with audit_service.guard(db, operator=operator, action="auth.disconnect",
                                 target_kind="account",
                                 target_id=str(accountId or "")):
            result = account_service.disconnect(db, accountId)
        action_detail = {"deleted": False}
    db.commit()
    audit_service.record(db, operator=operator, action="auth.disconnect",
                         target_kind="account", target_id=str(accountId or ""),
                         detail=action_detail)
    db.commit()
    return result


@router.get("/accounts", summary="已保存账号列表")
def accounts(db: DbSession) -> dict[str, Any]:
    rows = account_service.account_summaries(db)
    return {"accounts": rows, "total": len(rows)}


@router.delete("/accounts/{account_id}", summary="删除已保存账号")
def delete_account(account_id: str, db: DbSession,
                   operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    with audit_service.guard(db, operator=operator, action="auth.disconnect",
                             target_kind="account", target_id=str(account_id)):
        result = account_service.delete_account(db, account_id)
    db.commit()
    # 删号是**不可逆**的：对方 AppKey 只能由账号主人重新生成一次。
    # 所以这条审计的 detail 里带 deleted=True，和「只是断开」区分开。
    audit_service.record(db, operator=operator, action="auth.disconnect",
                         target_kind="account", target_id=str(account_id),
                         detail={"deleted": True})
    db.commit()
    return result


class RenameAccountRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


@router.patch("/accounts/{account_id}", summary="重命名已保存的连接")
def rename_account(account_id: str, payload: RenameAccountRequest, db: DbSession,
                   operator: OperatorDep) -> dict[str, Any]:
    require_writable(db)
    with audit_service.guard(db, operator=operator, action="auth.rename",
                             target_kind="account", target_id=str(account_id)):
        row = account_service.rename(db, account_id, payload.name)
    db.commit()
    audit_service.record(db, operator=operator, action="auth.rename",
                         target_kind="account", target_id=str(account_id),
                         detail={"name": str(payload.name or "")[:64]})
    db.commit()
    return {"account": row}
