"""自定义分组（精品 / 待改价 之类）。一个商品最多属于一个分组。"""
from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 前端色板键；用「组内库存最大值 + 1」作为默认排序位
    color: Mapped[str] = mapped_column(String(16), default="slate")
    sort: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts)
    updated_at: Mapped[float] = mapped_column(Float, default=now_ts, onupdate=now_ts)
