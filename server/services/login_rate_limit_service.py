"""Persistent attempt throttling for the public password login endpoint."""
from __future__ import annotations

import hashlib
import hmac
import time

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.config import get_settings
from db.session import SessionLocal
from models.login_rate_limit import LoginRateLimit

WINDOW_SECONDS = 15 * 60
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60


def _bucket_key(kind: str, value: str) -> str:
    secret = get_settings().local_secret().encode("utf-8")
    return hmac.new(secret, f"{kind}\0{value}".encode(), hashlib.sha256).hexdigest()


def bucket_keys(ip: str, username: str) -> tuple[str, str]:
    return _bucket_key("ip", ip), _bucket_key("username", username)


def _session_insert(db):
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite_insert(LoginRateLimit)
    if dialect == "postgresql":
        return pg_insert(LoginRateLimit)
    raise RuntimeError(f"Unsupported database for login throttling: {dialect}")


def blocked_retry_after(ip: str, username: str) -> int:
    """Return the active lock duration without charging successful logins."""
    now = time.time()
    keys = bucket_keys(ip, username)
    with SessionLocal() as db:
        until = db.scalar(
            select(LoginRateLimit.locked_until)
            .where(LoginRateLimit.bucket_key.in_(keys), LoginRateLimit.locked_until > now)
            .order_by(LoginRateLimit.locked_until.desc())
            .limit(1)
        )
    return max(0, int(until - now + 0.999)) if until else 0


def record_failure(ip: str, username: str) -> None:
    """Charge failed authentication to both the source IP and account buckets."""
    now = time.time()
    cutoff = now - WINDOW_SECONDS
    keys = bucket_keys(ip, username)
    with SessionLocal() as db:
        for key in keys:
            insert = _session_insert(db).values(
                bucket_key=key,
                window_started_at=now,
                failure_count=1,
                locked_until=0,
            )
            previous = LoginRateLimit.__table__
            reset = previous.c.window_started_at <= cutoff
            capped_count = case(
                (reset, 1),
                (previous.c.failure_count >= MAX_FAILURES, previous.c.failure_count),
                else_=previous.c.failure_count + 1,
            )
            next_count = case((reset, 1), else_=previous.c.failure_count + 1)
            insert = insert.on_conflict_do_update(
                index_elements=[LoginRateLimit.bucket_key],
                set_={
                    "window_started_at": case(
                        (reset, now), else_=previous.c.window_started_at
                    ),
                    "failure_count": capped_count,
                    "locked_until": case(
                        (previous.c.locked_until > now, previous.c.locked_until),
                        (reset, 0),
                        (next_count >= MAX_FAILURES, now + LOCK_SECONDS),
                        else_=0,
                    ),
                },
            )
            db.execute(insert)
        db.commit()


def clear_username(username: str) -> None:
    """Clear only the successful account's bucket; keep shared-IP failures."""
    key = _bucket_key("username", username)
    with SessionLocal() as db:
        db.execute(delete(LoginRateLimit).where(LoginRateLimit.bucket_key == key))
        db.commit()


def clear(ip: str, username: str) -> None:
    """Clear a test/administrative pair of buckets explicitly."""
    keys = bucket_keys(ip, username)
    with SessionLocal() as db:
        db.execute(delete(LoginRateLimit).where(LoginRateLimit.bucket_key.in_(keys)))
        db.commit()


def clear_ip(ip: str) -> None:
    """Clear a shared source-IP bucket, primarily for isolated tests."""
    key = _bucket_key("ip", ip)
    with SessionLocal() as db:
        db.execute(delete(LoginRateLimit).where(LoginRateLimit.bucket_key == key))
        db.commit()
