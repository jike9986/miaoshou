"""妙手账号凭据。AppSecret 用本机 secret 加密后落库，永不出现在响应里。

时间字段统一存 Unix 秒（REAL），与 docs/api.md 0.1 约定一致。
"""
from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id", ondelete="SET NULL"))
    bound_by_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id", ondelete="SET NULL"))
    bound_at: Mapped[float | None] = mapped_column(Float)
    # 操作者可见的连接名，唯一（应用层按 casefold 判重）
    name: Mapped[str] = mapped_column(String(64), default="")
    app_key: Mapped[str] = mapped_column(String(256), default="")
    app_secret_enc: Mapped[str] = mapped_column(Text, default="")
    # 末 4 位，用于界面区分账号；完整密钥永不下行
    secret_hint: Mapped[str] = mapped_column(String(8), default="")
    app_id_hint: Mapped[str] = mapped_column(String(16), default="")
    site: Mapped[str] = mapped_column(String(8), default="MY")
    is_default: Mapped[bool] = mapped_column(default=False)
    # active / invalid / rate_limited
    status: Mapped[str] = mapped_column(String(16), default="active")
    # 店铺列表缓存（JSON 数组文本）
    shops: Mapped[str] = mapped_column(Text, default="[]")
    shops_synced_at: Mapped[float | None] = mapped_column(Float)
    last_verified_at: Mapped[float | None] = mapped_column(Float)
    last_used: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts)
    updated_at: Mapped[float] = mapped_column(Float, default=now_ts, onupdate=now_ts)

    __table_args__ = (
        Index("ix_accounts_status", "status"),
        Index("ix_accounts_name", "name"),
        Index("ix_accounts_default", "is_default"),
        Index(
            "uq_accounts_app_key_nonempty",
            "app_key",
            unique=True,
            sqlite_where=(app_key != ""),
            postgresql_where=(app_key != ""),
        ),
    )
