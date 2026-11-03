"""认证路由：连接 / 状态 / 断开 / 账号管理。"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError

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
from services import (
    account_service,
    audit_service,
    email_code_service,
    email_service,
    login_rate_limit_service,
    operator_service,
    setting_service,
)

router = APIRouter(prefix="/auth", tags=["认证"])
login_router = APIRouter(prefix="/auth", tags=["认证"])
logger = logging.getLogger(__name__)

class LoginRequest(BaseModel):
    # `username` is the original client field. `identifier` is the current UI
    # contract; `email` is also accepted for conventional API clients.
    username: str | None = Field(default=None, max_length=254)
    identifier: str | None = Field(default=None, max_length=254)
    email: str | None = Field(default=None, max_length=254)
    password: str = Field(max_length=128)

    @model_validator(mode="after")
    def validate_identifier(self) -> LoginRequest:
        values = [value.strip() for value in (self.identifier, self.email, self.username)
                  if value and value.strip()]
        if not values:
            raise ValueError("请输入用户名或邮箱")
        if len({value.casefold() for value in values}) > 1:
            raise ValueError("用户名、邮箱和登录标识不能填写不同的值")
        return self

    @property
    def login_identifier(self) -> str:
        return self.identifier or self.email or self.username or ""


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    code: str = Field(pattern=r"^\d{6}$")


class EmailCodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class EmailCodeLoginConfirmRequest(EmailCodeRequest):
    code: str = Field(pattern=r"^\d{6}$")


class EmailCodeConfirmRequest(EmailCodeRequest):
    code: str = Field(pattern=r"^\d{6}$")


class ChangePasswordRequest(BaseModel):
    currentPassword: str = Field(max_length=128)
    newPassword: str = Field(min_length=8, max_length=128)


class CompletePasswordSetupRequest(BaseModel):
    newPassword: str = Field(min_length=8, max_length=128)


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class PasswordResetConfirmRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(pattern=r"^\d{6}$")
    newPassword: str = Field(min_length=8, max_length=128)


class InitializeCredentialsRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(min_length=8, max_length=128)


def _check_email_code_verify_limit(ip: str, email: str, purpose: str) -> str:
    namespace = f"email-code-{purpose}-verify"
    retry_after = login_rate_limit_service.blocked_retry_after(
        ip, email, namespace=namespace)
    if retry_after:
        raise errors.AppError(
            errors.RATE_LIMITED, "验证码验证失败次数过多，请稍后重试", status=429,
            details={"retryAfterSeconds": retry_after})
    return namespace


def _record_email_code_verify_failure(ip: str, email: str, *, namespace: str) -> None:
    login_rate_limit_service.record_failure(
        ip, email, namespace=namespace,
        ip_limit=email_code_service.EMAIL_CODE_IP_VERIFY_FAILURE_LIMIT,
        username_limit=email_code_service.MAX_CODE_ATTEMPTS,
    )


@login_router.post("/login")
def login(payload: LoginRequest, request: Request, db: DbSession) -> dict[str, Any]:
    from core.errors import RATE_LIMITED, AppError
    from services import login_rate_limit_service

    try:
        username = operator_service.normalize_login_identifier(payload.login_identifier)
    except Exception:
        username = payload.login_identifier.strip().casefold()
    ip = request.client.host if request.client else "unknown"
    retry_after = login_rate_limit_service.blocked_retry_after(ip, username)
    if retry_after:
        raise AppError(RATE_LIMITED, "登录失败次数过多，请稍后重试", status=429,
                       details={"retryAfterSeconds": retry_after})

    result = operator_service.authenticate_password(
        db, payload.login_identifier, payload.password)
    if result is None:
        login_rate_limit_service.record_failure(ip, username)
        raise AppError("AUTH_FAILED", "邮箱、用户名或密码错误", status=401)
    operator, token = result
    login_rate_limit_service.clear_username(username)
    db.commit()
    return {"operator": {"id": str(operator.id),
                          "name": operator_service.serialize(operator)["name"],
                          "role": (operator_service.ROLE_ADMIN
                                   if operator_service.is_superadmin(db, operator)
                                   else operator_service.role_of(operator))},
            "token": token, "mustChangePassword": operator.must_change_password}


@login_router.post("/email-code-login/request", status_code=202)
def request_email_code_login(payload: EmailCodeRequest, request: Request,
                             db: DbSession,
                             background_tasks: BackgroundTasks) -> dict[str, bool]:
    """Request a sign-in code without disclosing whether an address can log in."""
    email = operator_service.normalize_deliverable_email(payload.email)
    email_service.ensure_configured()
    ip = request.client.host if request.client else "unknown"
    purpose = email_code_service.PURPOSE_EMAIL_LOGIN
    email_code_service.enforce_send_limit(ip, purpose, email)

    target = (db.query(Operator)
              .filter(func.lower(func.trim(Operator.email)) == email,
                      Operator.enabled.is_(True),
                      Operator.email_verified.is_(True))
              .one_or_none())
    code = email_code_service.issue(db, purpose=purpose, email=email)
    db.commit()
    if target is not None:
        background_tasks.add_task(
            _deliver_email_login_code,
            recipient=email,
            display_name=target.name,
            code=code,
        )
    return {"ok": True}


@login_router.post("/email-code-login/confirm")
def confirm_email_code_login(payload: EmailCodeLoginConfirmRequest,
                             request: Request,
                             db: DbSession) -> dict[str, Any]:
    """Consume a one-time login code and rotate the account's active session."""
    email = operator_service.normalize_deliverable_email(payload.email)
    purpose = email_code_service.PURPOSE_EMAIL_LOGIN
    invalid_code = errors.AppError("AUTH_FAILED", "邮箱或验证码错误", status=401)
    ip = request.client.host if request.client else "unknown"
    verify_namespace = _check_email_code_verify_limit(ip, email, purpose)
    if not email_code_service.consume(
            db, purpose=purpose, email=email, code=payload.code):
        db.commit()
        _record_email_code_verify_failure(ip, email, namespace=verify_namespace)
        raise invalid_code
    result = operator_service.authenticate_verified_email(db, email)
    if result is None:
        db.commit()
        _record_email_code_verify_failure(ip, email, namespace=verify_namespace)
        raise invalid_code
    operator, token = result
    db.commit()
    login_rate_limit_service.clear_username(email, namespace=verify_namespace)
    return {
        "operator": {
            "id": str(operator.id),
            "name": operator_service.serialize(operator)["name"],
            "role": (operator_service.ROLE_ADMIN
                     if operator_service.is_superadmin(db, operator)
                     else operator_service.role_of(operator)),
        },
        "token": token,
        "mustChangePassword": operator.must_change_password,
    }


def _deliver_email_login_code(*, recipient: str, display_name: str,
                              code: str) -> None:
    """Deliver sign-in mail without exposing delivery or account status publicly."""
    try:
        email_service.send_verification_code(
            recipient=recipient,
            display_name=display_name,
            code=code,
            purpose=email_code_service.PURPOSE_EMAIL_LOGIN,
            expires_minutes=email_code_service.CODE_TTL_SECONDS // 60,
        )
    except Exception as exc:  # pragma: no cover - exercised by background runner
        logger.warning("email login code delivery failed: %s", type(exc).__name__)


@login_router.post("/register", status_code=201)
def register(payload: RegisterRequest, request: Request,
             db: DbSession) -> dict[str, Any]:
    """Public email registration creates a least-privileged student account."""
    email = operator_service.normalize_deliverable_email(payload.email)
    ip = request.client.host if request.client else "unknown"
    verify_namespace = _check_email_code_verify_limit(
        ip, email, email_code_service.PURPOSE_REGISTRATION)
    invalid_code = errors.AppError(
        "AUTH_FAILED", "邮箱验证码无效或已过期", status=400)
    if not email_code_service.consume(
            db, purpose=email_code_service.PURPOSE_REGISTRATION,
            email=email, code=payload.code):
        db.commit()  # Persist invalid-code attempts and the five-attempt cap.
        _record_email_code_verify_failure(ip, email, namespace=verify_namespace)
        raise invalid_code
    # Consume a valid code before checking mailbox ownership so a duplicate
    # registration has the same public response as an invalid code.
    if (db.query(Operator.id)
            .filter(func.lower(func.trim(Operator.email)) == email)
            .first() is not None):
        db.commit()
        login_rate_limit_service.clear_username(email, namespace=verify_namespace)
        raise invalid_code

    try:
        operator, token = operator_service.register_email_account(
            db, payload.name, email, payload.password)
        db.commit()
        login_rate_limit_service.clear_username(email, namespace=verify_namespace)
    except errors.AppError as exc:
        if exc.code != "CONFLICT":
            raise
        # A concurrent registration can win after the ownership check. The
        # service may have rolled the transaction back, so consume again; if
        # the original mark is still pending this is a harmless no-op.
        email_code_service.consume(
            db, purpose=email_code_service.PURPOSE_REGISTRATION,
            email=email, code=payload.code)
        db.commit()
        raise invalid_code from None
    except IntegrityError:
        db.rollback()
        # Treat a unique-email race like the ordinary duplicate path. Do not
        # hide unrelated integrity failures such as a display-name collision.
        if (db.query(Operator.id)
                .filter(func.lower(func.trim(Operator.email)) == email)
                .first() is None):
            raise
        email_code_service.consume(
            db, purpose=email_code_service.PURPOSE_REGISTRATION,
            email=email, code=payload.code)
        db.commit()
        raise invalid_code from None
    return {"operator": {"id": str(operator.id),
                          "name": operator_service.serialize(operator)["name"],
                          "role": operator_service.role_of(operator),
                          "emailVerified": operator_service.serialize(operator)["emailVerified"]},
            "token": token, "mustChangePassword": False}


@login_router.post("/registration-code/request", status_code=202)
def request_registration_code(payload: EmailCodeRequest, request: Request,
                              db: DbSession) -> dict[str, bool]:
    email = operator_service.normalize_deliverable_email(payload.email)
    ip = request.client.host if request.client else "unknown"
    email_code_service.enforce_send_limit(
        ip, email_code_service.PURPOSE_REGISTRATION, email)
    code = email_code_service.issue(
        db, purpose=email_code_service.PURPOSE_REGISTRATION, email=email)
    try:
        email_service.send_verification_code(
            recipient=email, display_name="", code=code,
            purpose=email_code_service.PURPOSE_REGISTRATION,
            expires_minutes=email_code_service.CODE_TTL_SECONDS // 60,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"ok": True}


@router.post("/email-verification/request", status_code=202)
def request_email_verification(payload: EmailCodeRequest, request: Request,
                               db: DbSession,
                               operator: OperatorDep,
                               background_tasks: BackgroundTasks) -> dict[str, bool]:
    """Request a code to bind or verify the signed-in user's recovery mailbox."""
    email = operator_service.normalize_deliverable_email(payload.email)
    email_service.ensure_configured()
    ip = request.client.host if request.client else "unknown"
    email_code_service.enforce_send_limit(
        ip, email_code_service.PURPOSE_EMAIL_VERIFICATION, email)

    existing = (db.query(Operator.id)
                .filter(func.lower(func.trim(Operator.email)) == email,
                        Operator.id != operator.id)
                .first())
    if existing is not None:
        # Keep the response indistinguishable without sending mail to another
        # user's address or invalidating their own verification challenge.
        return {"ok": True}

    code = email_code_service.issue(
        db, purpose=email_code_service.PURPOSE_EMAIL_VERIFICATION, email=email)
    db.commit()
    background_tasks.add_task(
        _deliver_email_verification_code,
        recipient=email,
        display_name=operator.name,
        code=code,
    )
    return {"ok": True}


@router.post("/email-verification/confirm")
def confirm_email_verification(payload: EmailCodeConfirmRequest,
                               request: Request,
                               db: DbSession,
                               operator: OperatorDep) -> dict[str, Any]:
    email = operator_service.normalize_deliverable_email(payload.email)
    ip = request.client.host if request.client else "unknown"
    verify_namespace = _check_email_code_verify_limit(
        ip, email, email_code_service.PURPOSE_EMAIL_VERIFICATION)
    if not email_code_service.consume(
            db, purpose=email_code_service.PURPOSE_EMAIL_VERIFICATION,
            email=email, code=payload.code):
        db.commit()
        _record_email_code_verify_failure(ip, email, namespace=verify_namespace)
        raise errors.AppError("AUTH_FAILED", "邮箱验证码无效或已过期", status=400)
    # Consume a correct code before checking ownership so that a mailbox already
    # bound elsewhere is indistinguishable from an invalid/expired challenge.
    if (db.query(Operator.id)
            .filter(func.lower(func.trim(Operator.email)) == email,
                    Operator.id != operator.id)
            .first() is not None):
        db.commit()
        raise errors.AppError("AUTH_FAILED", "邮箱验证码无效或已过期", status=400)

    operator.email = email
    operator.email_verified = True
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise errors.AppError("AUTH_FAILED", "邮箱验证码无效或已过期", status=400) from None
    login_rate_limit_service.clear_username(email, namespace=verify_namespace)
    return {"ok": True, "email": operator_service.mask_email(email),
            "emailVerified": True}


def _deliver_email_verification_code(*, recipient: str, display_name: str,
                                     code: str) -> None:
    """Deliver a verification code after the public response has completed."""
    try:
        email_service.send_verification_code(
            recipient=recipient,
            display_name=display_name,
            code=code,
            purpose=email_code_service.PURPOSE_EMAIL_VERIFICATION,
            expires_minutes=email_code_service.CODE_TTL_SECONDS // 60,
        )
    except Exception as exc:  # pragma: no cover - exercised by background runner
        logger.warning("email verification code delivery failed: %s",
                       type(exc).__name__)


@login_router.post("/password-reset/request", status_code=202)
def request_password_reset(payload: PasswordResetRequest, request: Request,
                           db: DbSession,
                           background_tasks: BackgroundTasks) -> dict[str, bool]:
    """Send a short-lived recovery code without exposing account existence."""
    email = operator_service.normalize_deliverable_email(payload.email)
    email_service.ensure_configured()
    ip = request.client.host if request.client else "unknown"
    email_code_service.enforce_send_limit(
        ip, email_code_service.PURPOSE_PASSWORD_RESET, email)

    target = (db.query(Operator)
              .filter(func.lower(func.trim(Operator.email)) == email,
                      Operator.enabled.is_(True),
                      Operator.email_verified.is_(True))
              .one_or_none())
    # Persist the replacement for both known and unknown addresses.  This keeps
    # the request path the same length without revealing whether an account
    # exists; only a known account gets a background delivery task.
    code = email_code_service.issue(
        db, purpose=email_code_service.PURPOSE_PASSWORD_RESET, email=email)
    db.commit()
    if target is not None:
        background_tasks.add_task(
            _deliver_password_reset_code,
            recipient=email,
            display_name=target.name,
            code=code,
        )
    return {"ok": True}


def _deliver_password_reset_code(*, recipient: str, display_name: str,
                                 code: str) -> None:
    """Deliver recovery mail after the public request has completed.

    Recovery requests deliberately do not expose SMTP failures or account
    existence.  Log only the exception class so credentials, addresses and
    server responses never enter application logs.
    """
    try:
        email_service.send_verification_code(
            recipient=recipient,
            display_name=display_name,
            code=code,
            purpose=email_code_service.PURPOSE_PASSWORD_RESET,
            expires_minutes=email_code_service.CODE_TTL_SECONDS // 60,
        )
    except Exception as exc:  # pragma: no cover - exercised by background runner
        logger.warning("password reset code delivery failed: %s", type(exc).__name__)


@login_router.post("/password-reset/confirm")
def confirm_password_reset(payload: PasswordResetConfirmRequest,
                           request: Request,
                           db: DbSession) -> dict[str, Any]:
    from core.errors import AppError

    email = operator_service.normalize_deliverable_email(payload.email)
    ip = request.client.host if request.client else "unknown"
    verify_namespace = _check_email_code_verify_limit(
        ip, email, email_code_service.PURPOSE_PASSWORD_RESET)
    if not email_code_service.consume(
            db, purpose=email_code_service.PURPOSE_PASSWORD_RESET,
            email=email, code=payload.code):
        db.commit()  # Persist invalid-code attempts and the five-attempt cap.
        _record_email_code_verify_failure(ip, email, namespace=verify_namespace)
        raise AppError("AUTH_FAILED", "找回密码验证码无效或已过期", status=400)
    target = (db.query(Operator)
              .filter(func.lower(func.trim(Operator.email)) == email,
                      Operator.enabled.is_(True),
                      Operator.email_verified.is_(True))
              .one_or_none())
    if target is None:
        db.commit()
        _record_email_code_verify_failure(ip, email, namespace=verify_namespace)
        raise AppError("AUTH_FAILED", "找回密码验证码无效或已过期", status=400)
    token = operator_service.set_password(
        db, target, payload.newPassword, must_change=False, rotate_token=True)
    db.commit()
    login_rate_limit_service.clear_username(email, namespace=verify_namespace)
    return {
        "ok": True,
        "operator": {"id": str(target.id),
                      "name": operator_service.serialize(target)["name"],
                      "role": operator_service.role_of(target)},
        "token": token,
        "mustChangePassword": False,
    }


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


@router.post("/complete-password-setup")
def complete_password_setup(payload: CompletePasswordSetupRequest,
                            db: DbSession,
                            operator: OperatorDep) -> dict[str, Any]:
    """Allow a newly created account to set its first password without the temporary one."""
    if not operator.must_change_password:
        raise errors.AppError("CONFLICT", "账号不需要设置初始密码", status=409)
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
    settings = get_settings()
    if settings.shared_mode:
        raise errors.AppError(errors.FORBIDDEN,
                              "共享服务不支持通过客户端初始化最高权限账号", status=403)

    _acquire_credential_initialization_lock(db)
    builtin = operator_service.ensure_default(db)
    initialized_admin = (db.query(Operator.id)
                         .filter(Operator.enabled.is_(True),
                                 Operator.name != operator_service.DEFAULT_OPERATOR_NAME,
                                 Operator.username.is_not(None),
                                 Operator.password_hash.is_not(None),
                                 Operator.role.in_((operator_service.ROLE_TEACHER,
                                                    operator_service.ROLE_ADMIN)))
                         .first())
    if (builtin.username is not None or builtin.password_hash is not None
            or initialized_admin is not None):
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
        db, payload.username, role=operator_service.ROLE_ADMIN)
    operator_service.set_password(
        db, operator, payload.password, must_change=False,
        username=username)
    db.commit()
    return {
        "ok": True,
        "operator": {"id": str(operator.id),
                     "name": operator_service.serialize(operator)["name"],
                     "role": operator_service.ROLE_ADMIN},
        "token": token,
        "mustChangePassword": False,
    }


def _acquire_credential_initialization_lock(db: Any) -> None:
    """Serialize HTTP bootstrap against another concurrent bootstrap request."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                   {"key": 684_210_771})
    elif dialect == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    else:
        raise RuntimeError(f"Unsupported database for credential initialization: {dialect}")


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
            # 未选择代绑学员时绑定给本人；共享部署同样允许老师使用自己的账号。
            owner = operator
        else:
            owner = operator_service.get(db, payload.studentOperatorId)
            if operator_service.role_of(owner) != operator_service.ROLE_MEMBER:
                raise errors.validation("妙手账号只能绑定到学员账号", field="studentOperatorId")
    # 一个妙手账号可以被多个学员绑定；每个学员只能有一条绑定。
    # 具体的单绑定约束在 account_service.connect 内再次校验，避免只靠
    # 请求参数检查造成并发下的重复绑定。
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
