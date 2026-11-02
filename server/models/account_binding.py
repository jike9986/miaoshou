"""Software-user bindings to a shared Miaoshou account."""
from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class AccountBinding(Base):
    __tablename__ = "account_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operators.id", ondelete="CASCADE"), nullable=False)
    bound_by_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id", ondelete="SET NULL"))
    bound_at: Mapped[float] = mapped_column(Float, default=now_ts, nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "operator_id", name="uq_account_binding_pair"),
        UniqueConstraint("operator_id", name="uq_account_binding_operator"),
        Index("ix_account_bindings_account", "account_id"),
    )
