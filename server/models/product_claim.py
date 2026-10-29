"""A student's claim on a shared finished product."""
from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class ProductClaim(Base):
    __tablename__ = "product_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    owner_operator_id: Mapped[int] = mapped_column(
        ForeignKey("operators.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="claimed", server_default="claimed", nullable=False
    )
    claimed_at: Mapped[float] = mapped_column(Float, default=now_ts, nullable=False)

    __table_args__ = (
        UniqueConstraint("product_id", "owner_operator_id", name="uq_product_claim_owner"),
        Index("ix_product_claim_owner_status", "owner_operator_id", "status"),
    )
