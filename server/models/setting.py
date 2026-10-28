"""键值设置（点分命名）。

已定义的键：
  workflow.defaults / pricing.templates / pricing.selected
  publish.enabled / image.limits / update.channel
  ui.lastView / security.readOnly / warehouse.customCategories
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base

READ_ONLY_KEY = "security.readOnly"
PUBLISH_ENABLED_KEY = "publish.enabled"
IMAGE_LIMITS_KEY = "image.limits"
PRICING_TEMPLATES_KEY = "pricing.templates"
PRICING_SELECTED_KEY = "pricing.selected"
WORKFLOW_DEFAULTS_KEY = "workflow.defaults"
UPDATE_CHANNEL_KEY = "update.channel"
UI_LAST_VIEW_KEY = "ui.lastView"
# 运营自定义的分类标签（名字数组）。老版存在 warehouse-categories.json，
# 新版一律落设置表：少一个需要备份/清理的运行时文件。
CUSTOM_CATEGORIES_KEY = "warehouse.customCategories"

DEFAULT_SETTINGS: dict[str, Any] = {
    READ_ONLY_KEY: False,
    PUBLISH_ENABLED_KEY: False,
    IMAGE_LIMITS_KEY: {"minute": 600, "hour": 6000, "day": 60000, "budgetMilliyuan": 10000},
    WORKFLOW_DEFAULTS_KEY: {
        "languages": ["ms"],
        "mainLimit": 9,
        "detailLimit": 9,
        "concurrency": 3,
        "translateImages": True,
        "processText": True,
        "weightIncrement": 0.1,
        "packageDimensions": {
            "packageLength": 30, "packageWidth": 20, "packageHeight": 10},
        "sizeChartUrl": "https://aohuang888.com/tk-product-automation/size-chart.png",
        "pricing": {},
        "publish": {},
    },
    PRICING_TEMPLATES_KEY: {},
    PRICING_SELECTED_KEY: {"accounts": {}},
    UPDATE_CHANNEL_KEY: {"manifestUrl": "", "channel": "stable"},
    UI_LAST_VIEW_KEY: "",
}


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 任意 JSON 结构（bool / number / object / array 都直接存）
    value: Mapped[Any] = mapped_column(JSON, default=None)
    updated_at: Mapped[float] = mapped_column(Float, default=now_ts, onupdate=now_ts)
