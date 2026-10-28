"""Pydantic v2 基类与通用信封：camelCase 自动转换、分页、错误。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

T = TypeVar("T")


class CamelModel(BaseModel):
    """入参/出参基类：snake_case 内部字段 ↔ camelCase JSON 字段。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
        ser_json_by_alias=True,
    )


class PageEnvelope(CamelModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    page: int = 1
    pageSize: int = 20
    total: int = 0
    hasMore: bool = False


class ErrorBody(CamelModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    requestId: str = ""


class ErrorEnvelope(CamelModel):
    error: ErrorBody


class OkEnvelope(CamelModel):
    ok: bool = True


class DeletedEnvelope(CamelModel):
    deleted: bool = True
    id: str | None = None
    productsUnassigned: int | None = None
    filesRemoved: int | None = None
    logsKept: int | None = None
    stats: dict[str, Any] | None = None
