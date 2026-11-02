"""Short-lived, single-use email verification codes."""
from __future__ import annotations

from sqlalchemy import Float, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class EmailVerificationCode(Base):
    __tablename__ = "email_verification_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    used_at: Mapped[float | None] = mapped_column(Float)
    failed_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False)
    __table_args__ = (
        Index("ix_email_codes_expires_at", "expires_at"),
        Index("ix_email_codes_lookup", "purpose", "email", "expires_at"),
        Index("uq_email_codes_active", "purpose", "email", unique=True,
              sqlite_where=text("used_at IS NULL"),
              postgresql_where=text("used_at IS NULL")),
    )
