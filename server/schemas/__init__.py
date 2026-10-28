"""schemas 包：请求/响应/信封。"""
from __future__ import annotations

from schemas.common import (
    CamelModel,
    DeletedEnvelope,
    ErrorBody,
    ErrorEnvelope,
    OkEnvelope,
    PageEnvelope,
)

__all__ = [
    "CamelModel",
    "DeletedEnvelope",
    "ErrorBody",
    "ErrorEnvelope",
    "OkEnvelope",
    "PageEnvelope",
]
