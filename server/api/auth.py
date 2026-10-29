"""认证路由：连接 / 状态 / 断开 / 账号管理。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from api.deps import (
    AdminDep,
    DbSession,
    OperatorDep,
    _same_secret,
    _token_from_headers,
    optional_account,
    require_permission,
    require_writable,
)
from core import errors
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
from models.operator import Operator
from schemas.requests import ConnectRequest
from services import account_service, audit_service, operator_service, setting_service

router = APIRouter(prefix="/auth", tags=["认证"])
login_router = APIRouter(prefix="/auth", tags=["认证"])


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)


class ChangePasswordRequest(BaseModel):
    currentPassword: str = Field(max_length=128)
    newPassword: str = Field(min_length=8, max_length=128)


class InitializeCredentialsRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(min_length=8, max_length=128)


@login_router.post("/login")
def login(payload: LoginRequest, db: DbSession) -> dict[str, Any]:
    result = operator_service.authenticate_password(db, payload.username, payload.password)
    if result is None:
        from core.errors import AppError
        raise AppError("AUTH_FAILED", "用户名或密码错误", status=401)
    operator, token = result
    db.commit()
    return {"operator": {"id": str(operator.id),
                          "name": operator_service.serialize(operator)["name"],
                          "role": operator_service.role_of(operator)},
            "token": token, "mustChangePassword": operator.must_change_password}


@login_router.get("/credentials-status")
def credentials_status(db: DbSession) -> dict[str, bool]:
    """Expose only whether one-time teacher login setup is still available."""
    initialized = (db.query(Operator.id)
                   .filter(Operator.username.is_not(None),
                           Operator.password_hash.is_not(None),
                           Operator.enabled.is_(True),
                           Operator.role.in_((operator_service.ROLE_TEACHER,
                                              operator_service.ROLE_ADMIN)))
                   .first() is not None)
    return {"initialized": initialized}


@router.post("/change-password")
def change_password(payload: ChangePasswordRequest, db: DbSession,
                    operator: OperatorDep) -> dict[str, Any]:
    if not operator_service.verify_password(payload.currentPassword, operator.password_hash):
        from core.errors import AppError
        raise AppError("AUTH_FAILED", "当前密码错误", status=401)
    token = operator_service.set_password(
        db, operator, payload.newPassword, must_change=False, rotate_token=True)
    db.commit()
    return {"ok": True, "token": token, "mustChangePassword": False}


@login_router.post("/initialize-credentials")
def initialize_credentials(
    payload: InitializeCredentialsRequest,
    db: DbSession,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_local_token: str | None = Header(default=None, alias="X-Local-Token"),
) -> dict[str, Any]:
    """One-time local takeover: retire the machine identity and create a teacher."""
    supplied = _token_from_headers(authorization, x_local_token)
    if not _same_secret(supplied, get_settings().local_secret()):
        raise errors.AppError(errors.AUTH_REQUIRED,
                              "首次接管必须使用本机令牌", status=401)
    builtin = operator_service.ensure_default(db)
    if builtin.username is not None or builtin.password_hash is not None:
        from core.errors import AppError
        raise AppError("CONFLICT", "登录凭据已初始化", status=409,
                       details={"reason": "credentials_initialized"})
    username = operator_service.normalize_username(payload.username)
    if db.query(Operator).filter(Operator.username == username).one_or_none():
        raise errors.validation("用户名已存在", field="username")

    # Retire the machine identity without changing its row: existing product and
    # publish records must keep their original actor instead of becoming teacher work.
    retired = (db.query(Operator)
               .filter(Operator.id == builtin.id,
                       Operator.username.is_(None),
                       Operator.password_hash.is_(None))
               .update({Operator.enabled: False,
                        Operator.is_admin: False,
                        Operator.role: operator_service.ROLE_MEMBER,
                        Operator.password_hash: "retired"},
                       synchronize_session=False))
    if retired != 1:
        raise errors.AppError("CONFLICT", "登录凭据已初始化", status=409,
                              details={"reason": "credentials_initialized"})

    operator, token = operator_service.create(
        db, payload.username, role=operator_service.ROLE_TEACHER)
    operator_service.set_password(
        db, operator, payload.password, must_change=False,
        username=username)
    db.commit()
    return {
        "ok": True,
        "operator": {"id": str(operator.id),
                     "name": operator_service.serialize(operator)["name"],
                     "role": operator_service.ROLE_TEACHER},
        "token": token,
        "mustChangePassword": False,
    }


@router.post("/connect", summary="连接妙手账号",
             dependencies=[require_permission("account.bind")])
def connect(payload: ConnectRequest, db: DbSession,
            operator: OperatorDep) -> dict[str, Any]:
    """连接/切换妙手账号。

    账号按学员归属；教师代绑时记录操作者和学员。审计只记账号名与 id，**不记 AppSecret**。
    """
    role = operator_service.role_of(operator)
    if role == operator_service.ROLE_MEMBER:
        if payload.studentOperatorId not in (None, "", str(operator.id)):
            raise errors.AppError(errors.FORBIDDEN, "学员只能绑定自己的妙手账号", status=403)
        owner = operator
    else:
        if role not in {operator_service.ROLE_TEACHER, operator_service.ROLE_ADMIN}:
            raise errors.AppError(errors.FORBIDDEN, "无权绑定妙手账号", status=403)
        if payload.studentOperatorId in (None, ""):
            raise errors.validation("代绑时必须指定学员账号", field="studentOperatorId")
        owner = operator_service.get(db, payload.studentOperatorId)
        if operator_service.role_of(owner) != operator_service.ROLE_MEMBER:
            raise errors.validation("妙手账号只能绑定到学员账号", field="studentOperatorId")
    if payload.accountId not in (None, ""):
        existing = account_service.get_account(db, payload.accountId)
        if (existing is not None
                and existing.owner_operator_id != owner.id
                and not (role in {operator_service.ROLE_ADMIN,
                                  operator_service.ROLE_TEACHER}
                         and existing.owner_operator_id is None)):
            # 教师可显式把未归属的历史 key 认领到学员；已有归属不可被抢占。
            raise errors.AppError(errors.FORBIDDEN,
                                  "妙手账号已绑定到其他学员", status=403)
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
            owner_operator_id=owner.id,
            bound_by_operator_id=operator.id,
        )
    db.commit()
    account = result.get("account") or {}
    audit_service.record(db, operator=operator, action="auth.connect",
                         target_kind="account",
                         target_id=str(account.get("id") or payload.accountId or ""),
                         detail={"name": str(account.get("name") or payload.name or "")[:64],
                                 "autoScan": bool(payload.autoScan),
                                 "saved": bool(payload.save),
                                 "ownerOperatorId": str(owner.id)})
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
    operator: AdminDep,
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
                   operator: AdminDep) -> dict[str, Any]:
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
                   operator: AdminDep) -> dict[str, Any]:
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
