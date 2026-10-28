"""路由公共工具：查询参数解析、分页参数校验、异常安全调用。"""
from __future__ import annotations

from typing import Any

from fastapi import Query

from core import errors


def parse_bool(value: Any, field: str, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise errors.validation(f"{field} 必须是布尔值（true / false）", field=field, value=value)


def parse_int(value: Any, field: str, default: int, *, low: int | None = None,
              high: int | None = None) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        raise errors.validation(f"{field} 必须是整数", field=field, value=value) from None
    if low is not None and number < low:
        raise errors.validation(f"{field} 不能小于 {low}", field=field, value=number)
    if high is not None and number > high:
        raise errors.validation(f"{field} 不能大于 {high}", field=field, value=number)
    return number


def parse_float(value: Any, field: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        raise errors.validation(f"{field} 必须是数字", field=field, value=value) from None


def page_params(
    page: int = Query(default=1, ge=1, description="页码，从 1 起"),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize",
                           description="每页数量"),
) -> tuple[int, int]:
    return page, page_size
