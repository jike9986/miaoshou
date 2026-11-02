"""Issue and verify single-use email codes for registration and recovery."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from sqlalchemy import case, delete, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from core import errors
from core.config import get_settings
from models.email_verification_code import EmailVerificationCode
from services import login_rate_limit_service

CODE_TTL_SECONDS = 15 * 60
MAX_CODE_ATTEMPTS = 5
CODE_LENGTH = 6
REGISTRATION_IP_SEND_LIMIT = 30
PURPOSE_REGISTRATION = "registration"
PURPOSE_PASSWORD_RESET = "password_reset"
PURPOSE_EMAIL_VERIFICATION = "email_verification"
_CLEANUP_INTERVAL_SECONDS = 60 * 60
_last_cleanup_at = 0.0
_cleanup_lock = threading.Lock()


def enforce_send_limit(ip: str, purpose: str, email: str) -> None:
    """Limit code requests by source IP and normalized mailbox."""
    bucket = f"email-code:{purpose}:{email}"
    namespace = f"email-code:{purpose}"
    ip_limit = (REGISTRATION_IP_SEND_LIMIT
                if purpose == PURPOSE_REGISTRATION
                else login_rate_limit_service.MAX_FAILURES)
    retry_after = login_rate_limit_service.reserve(
        ip, bucket, namespace=namespace, ip_limit=ip_limit)
    if retry_after:
        raise errors.AppError(
            errors.RATE_LIMITED, "验证码请求过于频繁，请稍后重试", status=429,
            details={"retryAfterSeconds": retry_after})
    # Reserve charges before delivery, so successful sends and unknown recovery
    # addresses receive the same protection as failed requests.


def issue(db: Session, *, purpose: str, email: str) -> str:
    """Persist a fresh code, replacing any earlier unused code for this email."""
    code = f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"
    now = time.time()
    _cleanup_expired(db, now)
    values = {
        "purpose": purpose,
        "email": email,
        "code_hash": _digest(purpose, email, code),
        "created_at": now,
        "expires_at": now + CODE_TTL_SECONDS,
        "used_at": None,
        "failed_attempts": 0,
    }
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        insert = sqlite_insert(EmailVerificationCode)
    elif dialect == "postgresql":
        insert = pg_insert(EmailVerificationCode)
    else:
        raise RuntimeError(f"Unsupported database for email verification codes: {dialect}")
    statement = insert.values(**values).on_conflict_do_update(
        index_elements=["purpose", "email"],
        index_where=text("used_at IS NULL"),
        set_={key: value for key, value in values.items()
              if key not in {"purpose", "email"}},
    )
    db.execute(statement)
    return code


def consume(db: Session, *, purpose: str, email: str, code: str) -> bool:
    """Consume a valid code exactly once, charging mismatches to its attempt cap."""
    now = time.time()
    row = (db.query(EmailVerificationCode)
           .filter(EmailVerificationCode.purpose == purpose,
                   EmailVerificationCode.email == email,
                   EmailVerificationCode.used_at.is_(None),
                   EmailVerificationCode.expires_at > now,
                   EmailVerificationCode.failed_attempts < MAX_CODE_ATTEMPTS)
           .one_or_none())
    if row is None:
        return False

    observed_hash = row.code_hash
    expected = _digest(purpose, email, code)
    if not hmac.compare_digest(observed_hash, expected):
        # Atomic increment avoids concurrent bad attempts bypassing the cap.
        db.execute(update(EmailVerificationCode).where(
            EmailVerificationCode.id == row.id,
            EmailVerificationCode.code_hash == observed_hash,
            EmailVerificationCode.used_at.is_(None),
            EmailVerificationCode.expires_at > now,
            EmailVerificationCode.failed_attempts < MAX_CODE_ATTEMPTS,
        ).values(
            failed_attempts=EmailVerificationCode.failed_attempts + 1,
            used_at=case(
                (EmailVerificationCode.failed_attempts + 1 >= MAX_CODE_ATTEMPTS, now),
                else_=None,
            ),
        ))
        return False

    result = db.execute(update(EmailVerificationCode).where(
        EmailVerificationCode.id == row.id,
        EmailVerificationCode.code_hash == expected,
        EmailVerificationCode.used_at.is_(None),
        EmailVerificationCode.expires_at > now,
        EmailVerificationCode.failed_attempts < MAX_CODE_ATTEMPTS,
    ).values(used_at=now))
    return result.rowcount == 1


def _cleanup_expired(db: Session, now: float) -> None:
    """Periodically remove expired and consumed challenges from the database."""
    global _last_cleanup_at
    if now - _last_cleanup_at < _CLEANUP_INTERVAL_SECONDS:
        return
    if not _cleanup_lock.acquire(blocking=False):
        return
    try:
        if now - _last_cleanup_at < _CLEANUP_INTERVAL_SECONDS:
            return
        db.execute(delete(EmailVerificationCode).where(
            (EmailVerificationCode.used_at.is_not(None))
            | (EmailVerificationCode.expires_at <= now)
        ))
        _last_cleanup_at = now
    finally:
        _cleanup_lock.release()


def _digest(purpose: str, email: str, code: str) -> str:
    key = get_settings().local_secret().encode("utf-8")
    message = f"email-code\0{purpose}\0{email}\0{code}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()
