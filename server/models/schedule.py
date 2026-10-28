"""定时发布任务。id 形如 `task-<uuid12>`，与旧数据兼容。

一天只跑一次（last_run_date），失败当天最多 3 次（attempts），由
services/schedule_service.py 的到期判断函数执行。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE")
    )
    shop_id: Mapped[str] = mapped_column(String(32), default="")
    template_name: Mapped[str] = mapped_column(String(64), default="")
    # daily / weekly / monthly / range
    frequency: Mapped[str] = mapped_column(String(16), default="daily")
    # weekly：1–7（周一为 1）
    days: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # monthly：1–31
    day_of_month: Mapped[list[Any]] = mapped_column(JSON, default=list)
    start_date: Mapped[str] = mapped_column(String(10), default="")
    end_date: Mapped[str] = mapped_column(String(10), default="")
    # 旧字段兼容：weekdays 与 days 同义
    weekdays: Mapped[list[Any]] = mapped_column(JSON, default=list)
    time_of_day: Mapped[str] = mapped_column(String(5), default="09:00")
    per_run: Mapped[int] = mapped_column(Integer, default=10)
    claim_only: Mapped[bool] = mapped_column(default=False)
    last_run_date: Mapped[str] = mapped_column(String(10), default="")
    attempt_date: Mapped[str] = mapped_column(String(10), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_due_at: Mapped[float | None] = mapped_column(Float)
    last_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_run_at: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts)
    updated_at: Mapped[float] = mapped_column(Float, default=now_ts, onupdate=now_ts)

    __table_args__ = (Index("ix_schedules_due", "next_due_at", "enabled"),)
