"""发布记录。只显示本机发起的记录；平台历史不在本表。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class PublishLog(Base):
    __tablename__ = "publish_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL")
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL")
    )
    schedule_id: Mapped[str | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL")
    )
    warehouse_key: Mapped[str] = mapped_column(String(64), default="")
    detail_id: Mapped[str] = mapped_column(String(32), default="")
    shop_id: Mapped[str] = mapped_column(String(32), default="")
    shop_name: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    # processing / success / fail / cancel
    status: Mapped[str] = mapped_column(String(16), default="processing")
    reason: Mapped[str] = mapped_column(Text, default="")
    platform_item_id: Mapped[str | None] = mapped_column(String(64))
    item_edit_url: Mapped[str | None] = mapped_column(Text)
    # direct / carrier
    mode: Mapped[str] = mapped_column(String(16), default="direct")
    batch_id: Mapped[str] = mapped_column(String(32), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 首次提交时间：一旦写下就不再改，重发只动 last_requested_at。
    # 两者一起看才知道「这单挂了多久」——只看最近一次提交永远是刚提交。
    requested_at: Mapped[float] = mapped_column(Float, default=now_ts)
    # 最近一次提交时间；存量库由 db/session.py 的轻量迁移补列，故允许为空
    last_requested_at: Mapped[float | None] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float)

    # 提交这条发布记录的人（operators.id）。发布列表要显示「谁发的」。
    # 可空 + 无默认值 + 无外键/索引，理由同 models/product.py 的 operator_id：
    # 老库走 `_add_missing_columns`（只支持 ADD COLUMN），NOT NULL 会直接失败，
    # 外键和索引在补列那条路上也加不上，加了只会让新旧库结构不一致。
    operator_id: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("account_id", "detail_id", "shop_id", name="uq_publish_logs_request"),
        Index("ix_publish_logs_time", "requested_at"),
        Index("ix_publish_logs_status", "status"),
        Index("ix_publish_logs_batch", "batch_id"),
    )
