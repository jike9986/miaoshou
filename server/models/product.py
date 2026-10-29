"""成品仓库里的商品（核心表）。

主图上限由 core.constants.MAIN_IMAGE_LIMIT 控制，入库时截断为 9 张。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    false,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 兼容旧键：{detail_id}:{shop_id}
    warehouse_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # detailId 只在账号内唯一
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL")
    )
    detail_id: Mapped[str] = mapped_column(String(32), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    shop_name: Mapped[str] = mapped_column(String(255), default="")
    source_shop_id: Mapped[str | None] = mapped_column(String(32))

    title: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(8), default="ms")
    site: Mapped[str] = mapped_column(String(8), default="")
    cover: Mapped[str] = mapped_column(Text, default="")

    cid: Mapped[str] = mapped_column(String(32), default="")
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    # 展示用中文类目名缓存（类目树可能被重建，缓存保证列表不空白）
    category_name: Mapped[str] = mapped_column(String(255), default="")
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"))
    group_name: Mapped[str] = mapped_column(String(64), default="")

    # 采购价（originPrice 的最小 / 最大值），selling price 取决于发布时模板
    price_min: Mapped[float | None] = mapped_column(Float)
    price_max: Mapped[float | None] = mapped_column(Float)
    price_currency: Mapped[str] = mapped_column(String(8), default="CNY")
    stock: Mapped[int] = mapped_column(Integer, default=0)
    sales_last_30: Mapped[int] = mapped_column(Integer, default=0)
    sku_count: Mapped[int] = mapped_column(Integer, default=0)

    # 主图（≤ MAIN_IMAGE_LIMIT）、详情图（≤ DETAIL_IMAGE_HARD_LIMIT）
    images: Mapped[list[Any]] = mapped_column(JSON, default=list)
    detail_images: Mapped[list[Any]] = mapped_column(JSON, default=list)
    sku_properties: Mapped[list[Any]] = mapped_column(JSON, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # ready / claimed / published / archived
    status: Mapped[str] = mapped_column(String(16), default="ready")
    automation_result: Mapped[bool] = mapped_column(
        default=False, server_default=false(), nullable=False)
    claimed: Mapped[bool] = mapped_column(default=False)
    claimed_at: Mapped[float | None] = mapped_column(Float)
    claimed_shop_id: Mapped[str | None] = mapped_column(String(32))
    collected: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    saved_at: Mapped[float] = mapped_column(Float, default=now_ts)
    updated_at: Mapped[float] = mapped_column(Float, default=now_ts, onupdate=now_ts)
    deleted_at: Mapped[float | None] = mapped_column(Float)

    # 最后一次改这件商品的人（operators.id）。列表页的「14:32 由张三修改」靠它。
    #
    # 三个刻意的选择：
    #   · **可空、无默认值**：老库靠 db/session.py 的 `_add_missing_columns` 补列，
    #     那个函数只支持 `ALTER TABLE ADD COLUMN`，`NOT NULL` 且无默认值会直接失败
    #     （355 件存量商品会让它无值可填）。可空是这里唯一安全的选择。
    #   · **不建外键**：建了也只有新库有（补列那条路加不上约束），
    #     同一个模型两种库两种结构，比没有外键更难排查。人删了这里留个悬空 id，
    #     读的时候查不到名字就显示空——不影响任何业务。
    #   · **不建索引**：同上，补列那条路加不上索引；而它的查询只有
    #     「按 id 批量取名」，量级是几十行，全表扫也无所谓。
    operator_id: Mapped[int | None] = mapped_column(Integer)

    # 乐观锁版本号（多人共用时防止静默互相覆盖）。
    #
    # 为什么需要它：warehouse_service.update() 的写法是「按 id 读出来 → 改字段 →
    # flush」，两次请求之间没有任何比对。A 和 B 同时打开同一件商品，A 先保存标题，
    # B 的页面还停在旧数据上，B 保存分组时会把 A 的标题一起盖回去——数据库里
    # 看不出异常，两个人都以为自己改成功了。
    #
    # 机制：每次写库 `revision += 1`；调用方带上自己读到的那一版
    # （`expected_revision`），对不上就 409 让前端刷新重来。
    #
    # 三个刻意的选择（与上面的 operator_id 同源）：
    #   · **default=1 + server_default="1"**：老库靠 db/session.py 的
    #     `_add_missing_columns` 补列，那条路只支持 `ALTER TABLE ADD COLUMN`，
    #     没有默认值的 NOT NULL 会直接失败（355 件存量商品无值可填）。
    #     给个标量默认值，补列时才能把存量行一起填成 1。
    #   · **nullable=False**：版本号不能为空，否则比对时 None 会把「没版本」
    #     和「版本对不上」混成一件事。
    #   · **不建索引**：它只在主键/唯一键定位到单行之后参与比对，不参与检索。
    revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "detail_id", "shop_id", name="uq_products_identity"),
        Index("ix_products_category", "category_id"),
        Index("ix_products_group", "group_id"),
        Index("ix_products_status", "status"),
        Index("ix_products_saved", "saved_at"),
    )
