"""统一错误模型：AppError 体系 + 错误码常量。

响应信封（docs/api.md 0.5）：

    {"error": {"code": "...", "message": "...", "details": {...}, "requestId": "req_..."}}

code 供前端分支，message 供人阅读，二者不得互相替代。
"""
from __future__ import annotations

from typing import Any

# --- 全局错误码 ------------------------------------------------------------
VALIDATION_ERROR = "VALIDATION_ERROR"
BAD_JSON = "BAD_JSON"
AUTH_REQUIRED = "AUTH_REQUIRED"
TOKEN_INVALID = "TOKEN_INVALID"
# 身份认出来了但没这个权限（管理员专属动作、不能停用自己等）。
# 与 TOKEN_INVALID 分开：401 是「不知道你是谁」，403 是「知道你是谁，但不许」。
FORBIDDEN = "FORBIDDEN"
FORBIDDEN_ORIGIN = "FORBIDDEN_ORIGIN"
READ_ONLY_MODE = "READ_ONLY_MODE"
NOT_FOUND = "NOT_FOUND"
CONFLICT_BUSY = "CONFLICT_BUSY"
DUPLICATE_NAME = "DUPLICATE_NAME"
UNPROCESSABLE = "UNPROCESSABLE"
RATE_LIMITED = "RATE_LIMITED"
INTERNAL_ERROR = "INTERNAL_ERROR"
UPSTREAM_ERROR = "UPSTREAM_ERROR"
UPSTREAM_UNKNOWN_RESULT = "UPSTREAM_UNKNOWN_RESULT"
UPSTREAM_RATE_LIMIT = "UPSTREAM_RATE_LIMIT"
UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"

# --- 业务错误码 ------------------------------------------------------------
CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
PUBLISH_DISABLED = "PUBLISH_DISABLED"
ALREADY_CLAIMED = "ALREADY_CLAIMED"
IMAGE_BUDGET_EXCEEDED = "IMAGE_BUDGET_EXCEEDED"
IMAGE_TOO_LARGE = "IMAGE_TOO_LARGE"
RETRY_UNSAFE = "RETRY_UNSAFE"
RESULT_FILE_MISSING = "RESULT_FILE_MISSING"
SITE_LANGUAGE_MISMATCH = "SITE_LANGUAGE_MISMATCH"
TEMPLATE_INVALID = "TEMPLATE_INVALID"
CARRIER_UNAVAILABLE = "CARRIER_UNAVAILABLE"
SHOP_NOT_IN_ACCOUNT = "SHOP_NOT_IN_ACCOUNT"
CATEGORY_NOT_LEAF = "CATEGORY_NOT_LEAF"
DIGEST_MISMATCH = "DIGEST_MISMATCH"
UPDATE_IN_PROGRESS = "UPDATE_IN_PROGRESS"
DUPLICATE_WORD = "DUPLICATE_WORD"
WORD_CONFLICT = "WORD_CONFLICT"
WORD_LIMIT_EXCEEDED = "WORD_LIMIT_EXCEEDED"
# 仅用于「账号密钥解不开」等需要与 AUTH_REQUIRED 区分的场景
ACCOUNT_SECRET_UNREADABLE = "ACCOUNT_SECRET_UNREADABLE"
# 乐观锁冲突：商品在你读到它之后被别人改过（products.revision 对不上）。
# 与 CONFLICT_BUSY 分开，是因为前端处置方式不同：BUSY 是「等一会儿再点」，
# REVISION_CONFLICT 是「重新拉一次这件商品，把当前值再改一遍」。
REVISION_CONFLICT = "REVISION_CONFLICT"


class AppError(Exception):
    """业务异常：携带 HTTP 状态码、稳定错误码与结构化细节。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details if details is not None else {}

    def envelope(self, request_id: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
        if request_id:
            body["requestId"] = request_id
        return {"error": body}


# --- 常用构造器 ------------------------------------------------------------
def validation(message: str, field: str | None = None, value: Any = None) -> AppError:
    details: dict[str, Any] = {}
    if field:
        details["field"] = field
        if value is not None:
            details["value"] = value
    return AppError(VALIDATION_ERROR, message, status=400, details=details)


def unprocessable(message: str, **details: Any) -> AppError:
    return AppError(UNPROCESSABLE, message, status=422, details=details)


def not_found(message: str, **details: Any) -> AppError:
    return AppError(NOT_FOUND, message, status=404, details=details)


def duplicate_name(message: str, **details: Any) -> AppError:
    return AppError(DUPLICATE_NAME, message, status=409, details=details)


def busy(message: str, **details: Any) -> AppError:
    return AppError(CONFLICT_BUSY, message, status=409, details=details)


def revision_conflict(message: str, *, expected: int | None = None,
                      actual: int | None = None, hint: str = "",
                      **details: Any) -> AppError:
    """乐观锁冲突：expected/actual 都给前端，hint 直接给人看。

    details 里固定带 `expected` / `actual` / `hint` 三个键，前端不必解析 message
    就能拿到「我手上是第几版、库里现在是第几版、该怎么办」。
    """
    payload: dict[str, Any] = dict(details)
    payload["expected"] = expected
    payload["actual"] = actual
    if hint:
        payload["hint"] = hint
    return AppError(REVISION_CONFLICT, message, status=409, details=payload)


def read_only() -> AppError:
    return AppError(
        READ_ONLY_MODE,
        "当前为只读模式，写操作已被拒绝；请在设置里关闭只读模式后重试",
        status=403,
    )


def forbidden(message: str, **details: Any) -> AppError:
    """403：身份已确认，但这个动作不允许。与 401 的区别见常量定义处。"""
    return AppError(FORBIDDEN, message, status=403, details=details)


def upstream(message: str, *, code: str = "", **details: Any) -> AppError:
    payload = dict(details)
    if code:
        payload.setdefault("upstream", {"code": code, "msg": message})
    return AppError(UPSTREAM_ERROR, message, status=502, details=payload)


def upstream_timeout(message: str = "妙手接口超时，请稍后重试") -> AppError:
    return AppError(UPSTREAM_TIMEOUT, message, status=504)


def upstream_rate_limit(message: str = "妙手接口限流，请稍后重试") -> AppError:
    return AppError(UPSTREAM_RATE_LIMIT, message, status=503)


def not_connected(
    message: str = "尚未连接妙手账号，请先在连接页填写 AppId / AppSecret",
) -> AppError:
    """未连接：契约要求 401 AUTH_REQUIRED，这里 message 说明补什么。"""
    return AppError(AUTH_REQUIRED, message, status=401,
                    details={"reason": "not_connected"})
