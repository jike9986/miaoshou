"""多设备会话 token。

当 Operator.multi_device = True 时，每次登录在此表新增一行而不是覆盖
operators.token。resolve 时先查此表，再查主表，两处任意命中即为有效会话。

明文 token 仅在登录响应里出现一次；此表只存 SHA-256 摘要。
"""
from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class OperatorToken(Base):
    __tablename__ = "operator_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operator_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("operators.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts)

    __table_args__ = (
        Index("ix_operator_tokens_operator_id", "operator_id"),
        Index("ix_operator_tokens_token_hash", "token_hash"),
    )
