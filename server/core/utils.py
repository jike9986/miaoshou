"""通用小工具：Unix 秒时间、金额十进制、脱敏、字段回退链。"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

_ID_SAFE = re.compile(r"[^0-9A-Za-z_.:-]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# --- 时间 -----------------------------------------------------------------
def now_ts() -> float:
    """Unix 秒（浮点，带毫秒）。"""
    return round(time.time(), 3)


def to_ts(value: Any) -> float | None:
    """把 datetime / date / 数字统一成 Unix 秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return round(value.timestamp(), 3)
    if isinstance(value, date):
        return round(datetime(value.year, value.month, value.day).timestamp(), 3)
    return None


def from_ts(value: Any) -> datetime | None:
    stamp = to_ts(value)
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp)


def clock_text(value: Any) -> str:
    """把时间戳格式化成「14:32:05」这种给人看的短时钟。

    只用在错误文案里（「这件商品刚被认领（14:32:05）」）：运营看到时间点就能
    判断「是不是我几秒前点的那一下」。取不到值就回空串，文案自己会省略括号。
    """
    moment = from_ts(value)
    if moment is None:
        return ""
    return moment.strftime("%H:%M:%S")


# --- 金额 -----------------------------------------------------------------
def money(value: Any) -> str | None:
    """金额统一字符串化，保留 2 位小数；无效值返回 None。"""
    if value in (None, "", False):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite():
        return None
    return str(number.quantize(Decimal("0.01")))


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, "", False):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def to_int(value: Any) -> int | None:
    if value in (None, "", False):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


# --- 文本 -----------------------------------------------------------------
def first_text(*values: Any) -> str:
    """字段回退链：返回第一个非空字符串（去首尾空格）。"""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def clean_text(value: Any, limit: int = 0) -> str:
    text = value.strip() if isinstance(value, str) else ""
    text = _CONTROL.sub("", text)
    if limit and len(text) > limit:
        text = text[:limit]
    return text


def image_url(value: Any) -> str:
    """从字符串 / dict / list 里取出第一个 http(s) 图片地址。"""
    if isinstance(value, str):
        return value if urlsplit(value).scheme in {"http", "https"} else ""
    if isinstance(value, Mapping):
        for key in ("url", "imgUrl", "imageUrl", "src", "mainImgUrl", "mainImageUrl"):
            found = image_url(value.get(key))
            if found:
                return found
        return ""
    if isinstance(value, (list, tuple)):
        for entry in value:
            found = image_url(entry)
            if found:
                return found
    return ""


def image_urls(value: Any, *, limit: int = 0) -> list[str]:
    """把任意图片容器整理成 http(s) 地址列表，去重并保序。"""
    found: list[str] = []
    if isinstance(value, (list, tuple)):
        for entry in value:
            candidate = image_url(entry)
            if candidate and candidate not in found:
                found.append(candidate)
    elif value:
        candidate = image_url(value)
        if candidate:
            found.append(candidate)
    if limit:
        found = found[:limit]
    return found


_DETAIL_IMG = re.compile(r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def detail_image_urls(notes: Any, *, limit: int = 0) -> list[str]:
    """从详情 HTML（notes）里按源顺序取出图片地址。"""
    if not isinstance(notes, str) or not notes:
        return []
    urls: list[str] = []
    for match in _DETAIL_IMG.finditer(notes):
        src = match.group(1).strip()
        if urlsplit(src).scheme in {"http", "https"} and src not in urls:
            urls.append(src)
    if limit:
        urls = urls[:limit]
    return urls


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """把 AppKey / AppSecret / token 从任何面向人的文本里抹掉。"""
    out = str(text or "")
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 4 and secret in out:
            out = out.replace(secret, "***")
    out = re.sub(r"(?i)bearer\s+[^\s,;\"']+", "Bearer ***", out)
    return re.sub(r"(?i)(app_?secret[\"']?\s*[:=]\s*)[^\s,;\"'}]+", r"\1***", out)


# --- 标识 -----------------------------------------------------------------
def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:26]


def new_batch_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def product_key(detail_id: Any, shop_id: Any) -> str:
    """仓库键：`{detailId}:{shopId}`。"""
    return f"{clean_text(str(detail_id))}:{clean_text(str(shop_id))}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_job_id(raw: bytes, model: str, prompt: str, size: str) -> str:
    """图片任务幂等 id：sha256(原图 + model + prompt + size)。"""
    digest = hashlib.sha256()
    digest.update(raw)
    digest.update((model + prompt + size).encode("utf-8"))
    return digest.hexdigest()


def safe_filename(name: str, default: str = "file") -> str:
    cleaned = _ID_SAFE.sub("_", str(name or "")).strip("._")
    return cleaned or default


def as_id_str(value: Any) -> str | None:
    """API 里的 id 一律字符串；None 保持 None。"""
    if value in (None, ""):
        return None
    return str(value)
