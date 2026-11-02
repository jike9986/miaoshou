"""一次性登录密码重置凭据。

只保存 token 摘要，明文只出现在发往用户邮箱的链接里。旧 token 在创建新
请求时失效，过期或使用过的 token 永远不能再次换取登录凭据。
"""
from __future__ import annotations

from sqlalchemy import Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operator_id: Mapped[int] = mapped_column(Integer, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    used_at: Mapped[float | None] = mapped_column(Float)
    requested_ip: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_password_reset_tokens_operator", "operator_id"),
        Index("ix_password_reset_tokens_active", "token_hash", "used_at", "expires_at"),
    )
