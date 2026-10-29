"""Persistent execution snapshots for the collect-box automation workflow."""
from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class AutomationRun(Base):
    __tablename__ = "automation_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner_operator_id: Mapped[int] = mapped_column(
        ForeignKey("operators.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="queued", server_default="queued", nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, default=now_ts, nullable=False)
    finished_at: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        Index("ix_automation_runs_owner_created", "owner_operator_id", "created_at"),
        Index("ix_automation_runs_account_shop", "account_id", "shop_id", "created_at"),
    )


class AutomationRunItem(Base):
    __tablename__ = "automation_run_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    detail_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_shop_id: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(
        String(500), default="", server_default=text("('')"), nullable=False)
    process_status: Mapped[str] = mapped_column(
        String(20), default="queued", server_default="queued", nullable=False)
    process_reason: Mapped[str] = mapped_column(
        Text, default="", server_default=text("('')"), nullable=False)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"))
    process_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    publish_status: Mapped[str] = mapped_column(
        String(20), default="not_requested", server_default="not_requested", nullable=False)
    publish_reason: Mapped[str] = mapped_column(
        Text, default="", server_default=text("('')"), nullable=False)
    publish_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, default=now_ts, nullable=False)

    __table_args__ = (
        UniqueConstraint("run_id", "key", name="uq_automation_run_item_key"),
        Index("ix_automation_run_items_run_status", "run_id", "process_status"),
    )
