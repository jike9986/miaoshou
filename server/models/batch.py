"""有状态批次（老版 `automation-batch.json` 的落库版）。

老版把整批状态塞在一个 JSON 文件里：`{status, items, currentIndex, settings,
errors}`。新版改为两张表，原因是 JSON 文件做不到两件关键的事：

  · **唯一约束**。`(batch_id, detail_id)` 上的 UNIQUE 是幂等闸的物理保证：
    同一批次里同一件商品只可能有一条记录，写重复直接由数据库拒掉，
    不依赖调用方或业务代码「记得去重」。
  · **并发安全**。逐件推进意味着多条请求会同时碰同一行，JSON 是整文件覆写，
    谁后写谁赢，进度会互相吃掉。

`status` 状态机（见 services/batch_service.py）：
    pending → running → done
                ├→ paused → running
                ├→ stopped
                └→ failed → running（retry_failed）
"""
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
)
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class Batch(Base):
    __tablename__ = "batches"

    # `bat_<uuid12>`，与 core.utils.new_batch_id 一致
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # pending / running / paused / stopped / done / failed
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    # 批次归属：一个账号 × 一个店铺。换店铺就是另一条生产链，不能混在一个批次里。
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL")
    )
    shop_id: Mapped[str] = mapped_column(String(32), default="")
    # 原始选择顺序（去重后）。BatchItem 里也有，这里留一份是为了「批次被清过
    # 子项」时仍能回答「这批本来要跑哪些」，也方便前端显示序号。
    detail_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # 指向 detail_ids / 子项顺序里的下一条。失败不推进，retry_failed 会把它拨回去。
    current_index: Mapped[int] = mapped_column(Integer, default=0)
    # 逐件处理参数（language / template / groupId / processImages / mainImageCount…）
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 每件的结果快照 [{detailId, status, message, at}]，给「人工核对」这一步看
    results: Mapped[list[Any]] = mapped_column(JSON, default=list)
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts)
    updated_at: Mapped[float] = mapped_column(Float, default=now_ts, onupdate=now_ts)
    finished_at: Mapped[float | None] = mapped_column(Float)

    # 建这批的人（operators.id）。批次列表要显示「谁建的」。
    # 可空 + 无默认值 + 无外键/索引，理由同 models/product.py 的 operator_id：
    # 老库走 `_add_missing_columns`（只支持 ADD COLUMN），NOT NULL 会直接失败。
    operator_id: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_batches_account_status", "account_id", "status"),
        Index("ix_batches_created", "created_at"),
    )


class BatchItem(Base):
    __tablename__ = "batch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    detail_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # pending / running / ok / failed / skipped
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    # 失败原因 / 跳过原因，直接给操作者看，不再另存一份 errors
    message: Mapped[str] = mapped_column(Text, default="")
    # 详情快照的 sha256。幂等闸：同一份快照已经提交成功过就不再提交第二次
    # （重复入仓会让重量被反复加增量，prepare_weight 不幂等）。
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    # 试过几次（失败累计）。retry_failed 不清零：要能看出「这件反复失败」。
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts)
    updated_at: Mapped[float] = mapped_column(Float, default=now_ts, onupdate=now_ts)

    __table_args__ = (
        # 幂等闸的物理保证：同一批次里同一件商品只允许一条
        UniqueConstraint("batch_id", "detail_id", name="uq_batch_items_detail"),
        Index("ix_batch_items_batch_status", "batch_id", "status"),
        Index("ix_batch_items_detail", "detail_id"),
    )
