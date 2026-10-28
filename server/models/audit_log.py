"""审计流水：谁、什么时候、对什么、做了什么、结果如何。

设计取舍：
  · **只记写操作**（认领 / 保存 / 发布 / 删除 / 改模板 / 改设置）。
    读操作不记——列表页一刷新就是几十条，记下来只会把真正的线索淹掉。
  · **operator_name 冗余存一份**。不冗余的话，操作员被删或改名之后，
    历史日志会显示成一个 NULL 或一个跟当时对不上的新名字，
    「谁做的」就答不出来了。审计表的第一原则是历史不可被后来的改动污染。
  · **operator_id 用 ON DELETE SET NULL**，不是 CASCADE：
    删人不能连带删掉他做过的事，否则等于给了「删号抹痕迹」的后门。
  · **绝不因为这里写失败就让业务失败**（见 services/audit_service.py）。

时间字段统一存 Unix 秒（REAL），与 docs/api.md 0.1 约定一致。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 操作员被删后置 NULL；历史仍可凭 operator_name 读出是谁
    operator_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("operators.id", ondelete="SET NULL"))
    # 冗余的名字快照：删号/改名之后历史仍然答得出「当时是谁」
    operator_name: Mapped[str] = mapped_column(String(64), default="")
    # "claim" / "save" / "publish" / "delete" / "template.save" ...
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    # "product" / "batch" / "template" / "schedule" / "system"
    target_kind: Mapped[str] = mapped_column(String(32), default="")
    # warehouse_key 或 batch id；空字符串表示「无具体对象」（如改共享设置）
    target_id: Mapped[str] = mapped_column(String(64), default="")
    # 关联的妙手账号 / 店铺：排查「发错店」时第一眼要看的就是这两列
    account_id: Mapped[int | None] = mapped_column(Integer)
    shop_id: Mapped[str] = mapped_column(String(32), default="")
    # "ok" / "failed" / "blocked"
    result: Mapped[str] = mapped_column(String(16), default="ok")
    # 失败原因摘要（错误码 + 人话），超长截断到 500
    message: Mapped[str] = mapped_column(String(500), default="")
    # 补充信息：数量、字段名、旧值新值等。不建表，直接塞 JSON。
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts)

    __table_args__ = (
        # 列表页：无条件按时间倒序翻页
        Index("ix_audit_logs_created", "created_at"),
        # 「张三最近干了什么」
        Index("ix_audit_logs_operator", "operator_id", "created_at"),
        # 「这个商品被谁动过」
        Index("ix_audit_logs_target", "target_kind", "target_id"),
    )
