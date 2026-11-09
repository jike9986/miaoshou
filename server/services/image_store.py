"""商品图片固化：把上游图片存到我们自己的图床。

背景（负责人 2026-11-02 明确指出）：仓库是「服务器上的商品库」，商品必须能脱离
妙手采集箱独立存在——采集箱一删、1688 一清，商品不能就没图了。之前只存了
1688 的 CDN 地址（`cbu01.alicdn.com/...`），那些地址随时可能失效。

做法：入库时把 payload 里所有图片地址（主图 / 详情图 / SKU 图 / 尺码图）
下载下来 → 上传到自己的图床（`aohuang888.com/tk-product-automation/...`）
→ 把 payload 里的地址替换成自己的。这样：
  · 采集箱删了、商品还在，图还在；
  · 任何账号认领/发布都能用这些图（不用再回 1688 抓）；
  · 同一张图按 URL 缓存，重复入库不会重复上传。

失败策略：单张图失败不阻断入库（记进 report），但**绝不用坏地址覆盖原地址**。
"""

from __future__ import annotations

import hashlib
import os
import logging
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

logger = logging.getLogger("aohuang.image_store")

# 只允许下载公网 http(s) 图，禁止内网地址（避免被当成 SSRF 跳板）
_PRIVATE_PREFIXES = (
    "127.", "10.", "192.168.", "169.254.", "0.",
    "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.",
    "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.",
    "172.28.", "172.29.", "172.30.", "172.31.",
)
MAX_IMAGE_BYTES = 15 * 1024 * 1024
DOWNLOAD_TIMEOUT = 20

# 已经是我们自己图床的地址前缀：这些不用再传
OWN_PUBLIC_HINTS = ("aohuang888.com",)


def _is_own_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(hint in host for hint in OWN_PUBLIC_HINTS)


def _is_public_http(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.username or parts.password:
        return False
    host = parts.hostname.lower()
    if host in ("localhost", "::1", "[::1]"):
        return False
    return not any(host.startswith(prefix) for prefix in _PRIVATE_PREFIXES)


def _download(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "AOHUANG-ImageStore/1.0"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:  # noqa: S310
        status = int(getattr(response, "status", 0) or 0)
        if status >= 400:
            raise ValueError(f"HTTP {status}")
        raw = response.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("图片超过 15 MB")
    if not raw:
        raise ValueError("图片内容为空")
    return raw


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class ImageStore:
    """一次入库会话内的图片固化器：带内存缓存，避免同一张图重复上传。"""

    def __init__(self, db: Session | None = None) -> None:
        self._memo: dict[str, str] = {}
        self._db = db

    def persist(self, url: Any) -> tuple[str, str]:
        """返回 (最终地址, 状态)。状态：kept / stored / skipped / failed。"""
        text = str(url or "").strip()
        if not text:
            return text, "skipped"
        # 测试/离线环境可以关掉真实下载：探针与单测用的是占位地址
        # （cbu01.alicdn.com/img/main-a.jpg 之类），真去下载只会得到 404，
        # 既拖慢用例又让「图片转存」看起来在破坏 payload（2026-11-09 实测）。
        if os.environ.get("AOHUANG_DISABLE_IMAGE_STORE", "").strip() in ("1", "true", "yes"):
            return text, "skipped"
        if not text.startswith(("http://", "https://")):
            return text, "skipped"
        if _is_own_url(text):
            return text, "kept"
        if text in self._memo:
            return self._memo[text], "stored"
        if not _is_public_http(text):
            return text, "skipped"
        try:
            from services import image_upload

            raw = _download(text)
            # 用内容哈希当文件名：同一张图（哪怕 URL 不同）只传一次
            digest = hashlib.sha256(raw).hexdigest()[:32]
            from pathlib import Path
            import tempfile

            suffix = ".jpg"
            if raw.startswith(b"\x89PNG"):
                suffix = ".png"
            elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
                suffix = ".webp"
            tmp = Path(tempfile.gettempdir()) / f"store-{digest}{suffix}"
            if not tmp.exists():
                tmp.write_bytes(raw)
            public = image_upload.upload_image(tmp, timeout=DOWNLOAD_TIMEOUT)
            self._memo[text] = public
            return public, "stored"
        except Exception as exc:  # noqa: BLE001 - 单张失败不阻断入库
            logger.warning("图片固化失败 url=%s：%s", text[:80], exc)
            return text, "failed"

    def persist_all(self, urls: Any) -> tuple[list[Any], dict[str, int]]:
        stats = {"kept": 0, "stored": 0, "skipped": 0, "failed": 0}
        if not isinstance(urls, list):
            return urls, stats
        out: list[Any] = []
        for item in urls:
            final, status = self.persist(item)
            stats[status] = stats.get(status, 0) + 1
            out.append(final)
        return out, stats


def persist_payload_images(payload: dict[str, Any]) -> dict[str, Any]:
    """把 payload 里的图片地址全部换成本地图床地址；返回统计报告。

    覆盖四处：主图 imgUrls、详情图 notes、SKU 图 skuPropertyList、尺码图 sizeChart。
    任何一处失败只记统计，不改变该处的原值。
    """
    from services import pre_save_check, warehouse_service

    report: dict[str, Any] = {"main": 0, "detail": 0, "sku": 0, "sizeChart": 0,
                              "failed": 0, "stored": 0}
    if not isinstance(payload, dict):
        return report
    store = ImageStore()

    def bump(stats: Mapping[str, int]) -> None:
        report["stored"] += int(stats.get("stored") or 0)
        report["failed"] += int(stats.get("failed") or 0)

    # 主图
    main = payload.get("imgUrls")
    if isinstance(main, list) and main:
        new_main, stats = store.persist_all(main)
        bump(stats)
        payload["imgUrls"] = new_main
        report["main"] = int(stats.get("stored") or 0)

    # 详情图（notes 是 HTML 片段，只替换其中的图片地址）
    notes = payload.get("notes")
    if isinstance(notes, str) and notes:
        detail_urls = pre_save_check.extract_detail_image_urls(notes)
        if detail_urls:
            new_notes = notes
            stored = 0
            for old in dict.fromkeys(detail_urls):
                final, status = store.persist(old)
                if status == "stored" and final != old:
                    new_notes = new_notes.replace(old, final)
                    stored += 1
                elif status == "failed":
                    report["failed"] += 1
            if stored:
                payload["notes"] = new_notes
                report["detail"] = stored
                report["stored"] += stored

    # SKU 图
    try:
        sku_sources = warehouse_service.sku_image_sources(payload)
    except Exception:  # noqa: BLE001
        sku_sources = []
    if sku_sources:
        replacements: dict[str, str] = {}
        for source in sku_sources:
            old = str(source.get("url") or "")
            final, status = store.persist(old)
            if status == "stored" and final != old:
                replacements[old] = final
                report["sku"] += 1
                report["stored"] += 1
            elif status == "failed":
                report["failed"] += 1
        if replacements:
            try:
                payload["skuPropertyList"] = warehouse_service.rewrite_sku_image_urls(
                    payload.get("skuPropertyList"), replacements)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SKU 图地址替换失败：%s", exc)

    # 尺码图
    chart = payload.get("sizeChart")
    if isinstance(chart, str) and chart.startswith(("http://", "https://")):
        final, status = store.persist(chart)
        if status == "stored" and final != chart:
            payload["sizeChart"] = final
            report["sizeChart"] = 1
            report["stored"] += 1
        elif status == "failed":
            report["failed"] += 1

    if report["stored"] or report["failed"]:
        logger.info("商品图片固化 已存=%s 失败=%s 明细=%s",
                    report["stored"], report["failed"],
                    {k: report[k] for k in ("main", "detail", "sku", "sizeChart")})
    return report
