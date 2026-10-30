from __future__ import annotations

from sqlalchemy import Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.session import Base


class LoginRateLimit(Base):
    __tablename__ = "login_rate_limits"

    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[float] = mapped_column(Float, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    locked_until: Mapped[float] = mapped_column(
        Float, nullable=False, default=0, server_default="0")

    __table_args__ = (Index("ix_login_rate_limits_locked_until", "locked_until"),)
