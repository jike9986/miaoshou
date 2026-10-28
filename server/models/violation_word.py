"""违规词库（标题清理用）。remove 与 review 不允许同词共存。"""
from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class ViolationWord(Base):
    __tablename__ = "violation_words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word: Mapped[str] = mapped_column(String(64), nullable=False)
    # casefold() 去重键
    normalized: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # remove / review
    action: Mapped[str] = mapped_column(String(8), default="remove")
    # manual / import / seed
    source: Mapped[str] = mapped_column(String(16), default="manual")
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts)

