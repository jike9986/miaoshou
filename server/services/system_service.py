"""系统：健康检查、版本与更新清单比对。

更新下载与静默安装交给 Electron 主进程；HTTP 接口只做清单比对与交接。
清单不可达时返回 200 + error 字段（旧行为保持一致），不抛 5xx。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.config import get_settings
from core.constants import (
    APP_NAME,
    APP_NAME_EN,
    APP_VERSION,
    UPDATE_CHANNEL,
    UPDATE_HOST_WHITELIST,
    UPDATE_MANIFEST_URL,
)
from core.utils import now_ts

_STARTED_AT = time.time()
TIMEOUT_SECONDS = 6.0


def health(db_ok: bool = True, db_state: str = "ok") -> dict[str, Any]:
    return {
        "ok": bool(db_ok),
        "name": "aohuang-listing-server",
        "version": APP_VERSION,
        "db": db_state,
        "uptimeSeconds": round(time.time() - _STARTED_AT, 1),
    }


def parse_version(value: Any) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(value or "").strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def _manifest_url(configured: str = "") -> str:
    return (configured or "").strip() or UPDATE_MANIFEST_URL


def fetch_manifest(url: str, *, timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    """拉取清单，不走系统代理（操作者机器上的 Clash 会让请求失败）。"""
    request = urllib.request.Request(
        url, headers={"User-Agent": f"AOHUANG-Listing/{APP_VERSION}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(256 * 1024)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest 不是对象")
    return data


def check_update(*, refresh: bool = True, manifest_url: str = "") -> dict[str, Any]:
    url = _manifest_url(manifest_url)
    base = {
        "name": APP_NAME,
        "nameEn": APP_NAME_EN,
        "current": APP_VERSION,
        "latest": "",
        "updateAvailable": False,
        "ok": False,
        "channel": UPDATE_CHANNEL,
        "manifestUrl": url,
        "platform": "win32-x64",
        "checkedAt": now_ts(),
    }
    try:
        manifest = fetch_manifest(url) if refresh else fetch_manifest(url)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        base["error"] = f"无法获取更新信息（{type(exc).__name__}）"
        return base
    latest = str(manifest.get("version") or "").strip()
    download = str(manifest.get("url") or "").strip()
    base.update({
        "ok": True,
        "latest": latest,
        "updateAvailable": bool(latest and download and is_newer(latest, APP_VERSION)),
        "url": download or None,
        "sha256": str(manifest.get("sha256") or "") or None,
        "sizeBytes": manifest.get("sizeBytes"),
        "notes": str(manifest.get("notes") or ""),
        "publishedAt": str(manifest.get("publishedAt") or ""),
        "channel": str(manifest.get("channel") or UPDATE_CHANNEL),
    })
    return base


def validate_update_request(payload: dict[str, Any]) -> dict[str, Any]:
    url = str(payload.get("url") or "").strip()
    if not url:
        from core import errors

        raise errors.validation("url 不能为空", field="url")
    parts = urlsplit(url)
    if parts.scheme != "https":
        from core import errors

        raise errors.validation("更新包地址必须是 https", field="url", value=url)
    if parts.hostname not in UPDATE_HOST_WHITELIST:
        from core import errors

        raise errors.AppError(
            "FORBIDDEN_ORIGIN",
            f"更新包域名 {parts.hostname} 不在白名单（aohuang888.com）",
            status=403, details={"host": parts.hostname},
        )
    sha256 = str(payload.get("sha256") or "").strip().lower()
    if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
        from core import errors

        raise errors.validation("sha256 必须是 64 位十六进制", field="sha256", value=sha256)
    return {
        "url": url,
        "sha256": sha256,
        "restart": payload.get("restart") is not False,
    }


def version_payload(*, refresh: bool = True, manifest_url: str = "") -> dict[str, Any]:
    return check_update(refresh=refresh, manifest_url=manifest_url)


def data_dir() -> str:
    return str(get_settings().data_dir)


def log_path() -> Path:
    return get_settings().logs_dir / "server.log"


def db_state(db: Any) -> str:
    """db 字段：ok / migrating / error。"""
    from sqlalchemy import text

    try:
        db.execute(text("SELECT 1"))
        return "ok"
    except Exception:  # noqa: BLE001 - 任何失败都视为 error
        return "error"
