"""店铺缓存（妙手 shop_list 的本地副本，供离线展示）。"""
from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="")
    site: Mapped[str] = mapped_column(String(8), default="")
    site_name: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32), default="")
    synced_at: Mapped[float] = mapped_column(Float, default=now_ts)

    __table_args__ = (
        UniqueConstraint("account_id", "shop_id", name="uq_shops_account_shop"),
        Index("ix_shops_account", "account_id"),
    )
