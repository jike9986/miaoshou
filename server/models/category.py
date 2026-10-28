"""平台类目树（妙手 cid / 名称 / 层级 / 面包屑）。

`account_id = 0` 作为「全局缓存」哨兵：SQLite 唯一索引把 NULL 视为不同值，
用 0 才能让「无账号时的类目树」同样受唯一约束保护（见 architecture.md 3.12）。
"""
from __future__ import annotations

from sqlalchemy import Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 0 表示全局缓存
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cid: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_cid: Mapped[str | None] = mapped_column(String(32))
    site: Mapped[str] = mapped_column(String(8), default="MY")
    name: Mapped[str] = mapped_column(String(255), default="")
    name_cn: Mapped[str] = mapped_column(String(255), default="")
    path: Mapped[str] = mapped_column(String(1024), default="")
    depth: Mapped[int] = mapped_column(Integer, default=0)
    is_leaf: Mapped[bool] = mapped_column(default=False)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    # 平台标记「这个类目当前不可选」。运营选了被禁用的类目，发布时才被拒，
    # 所以要在下拉里就能看出来（老版 category_library 的 disabled）。
    disabled: Mapped[bool] = mapped_column(default=False)
    # 妙手类目树里另带的两个 id，部分接口按它们定位类目
    aid: Mapped[str | None] = mapped_column(String(32))
    fid: Mapped[str | None] = mapped_column(String(32))
    synced_at: Mapped[float] = mapped_column(Float, default=now_ts)

    __table_args__ = (
        UniqueConstraint("account_id", "site", "cid", name="uq_categories_scope"),
        Index("ix_categories_cid", "cid"),
        Index("ix_categories_parent", "parent_cid"),
        Index("ix_categories_path", "path"),
    )
