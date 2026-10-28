"""定时任务执行历史（老版 `publish-schedule-log.json`）。

老版每次跑完都往一个 JSON 里追加一行，界面上能翻「昨天跑了几轮、每轮发了什么」。
新版此前只把最近一次结果覆写进 `schedules.last_result`——**上一轮做了什么永远
查不到**，出了问题只能看服务日志。

落库而不是写 JSON：JSON 每次追加都要整文件读写，任务多了会互相覆盖；
表结构还能按 task_id / 时间过滤。
"""
from __future__ import annotations

from sqlalchemy import Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class ScheduleLog(Base):
    __tablename__ = "schedule_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # 冗余存名字/账号：任务被删掉后历史仍要能读懂
    task_name: Mapped[str] = mapped_column(String(120), default="")
    account_id: Mapped[int | None] = mapped_column(Integer)
    # ok / partial / failed / empty
    status: Mapped[str] = mapped_column(String(16), default="")
    reason: Mapped[str] = mapped_column(String(500), default="")
    requested: Mapped[int] = mapped_column(Integer, default=0)
    selected: Mapped[int] = mapped_column(Integer, default=0)
    published: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    # 完整摘要（含 results / failures 明细），排查时不用再翻日志
    summary: Mapped[str] = mapped_column(String, default="")
    at: Mapped[float] = mapped_column(Float, default=now_ts)

    __table_args__ = (
        Index("ix_schedule_logs_task", "task_id"),
        Index("ix_schedule_logs_at", "at"),
    )
