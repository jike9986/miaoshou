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

from __future__ import annotations  # noqa: I001

import hashlib
import os
import re
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
            from pathlib import Path  # noqa: I001
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


def _drop_dead_detail_image(notes: str, dead_url: str) -> tuple[str, int]:
    """从详情 HTML 里删掉指向死链的 `<img>`；返回 (新 HTML, 删了几个)。

    死链 `<img>` 有三种写法要一起覆盖（妙手返回的 notes 经常在最后一张图上
    被截断，见 pre_save_check.extract_detail_image_urls 的说明）：

        <img src="URL">       完整
        <img src='URL' />     单引号 + 自闭合
        <img src="URL        残缺，没有结尾引号和尖括号

    URL 里含 `?` `!` `.` 等正则元字符（1688 图床就是 `_!!2216730257661-0-cib.jpg`），
    所以整段按 `re.escape` 精确匹配，不做模糊处理 —— 删错图比留着死链更糟。
    """
    if not notes or not dead_url:
        return notes, 0
    escaped = re.escape(dead_url)
    total = 0

    # 完整标签：<img ... src="URL" ...>
    complete = re.compile(
        r"<img\b[^>]*?\bsrc\s*=\s*([\"'])" + escaped + r"\1[^>]*>",
        re.IGNORECASE | re.DOTALL)
    notes, n = complete.subn("", notes)
    total += n

    # 残缺标签：URL 之后没有闭合引号/尖括号，删到字符串末尾或下一个 '<'
    broken = re.compile(
        r"<img\b[^>]*?\bsrc\s*=\s*([\"']?)" + escaped + r"(?![\w])[^<]*",
        re.IGNORECASE | re.DOTALL)
    notes, n = broken.subn("", notes)
    total += n

    # 兜底：裸露的地址本身（有些详情片段把 URL 直接写进 style/背景）
    if dead_url in notes:
        notes = notes.replace(dead_url, "")
        total += 1

    return notes, total


def persist_payload_images(payload: dict[str, Any]) -> dict[str, Any]:
    """把 payload 里的图片地址全部换成本地图床地址；返回统计报告。

    覆盖四处：主图 imgUrls、详情图 notes、SKU 图 skuPropertyList、尺码图 sizeChart。
    单张失败只记统计、不覆盖原值；但**详情图里的死链会被删掉** ——
    留着它，后面的图片翻译阶段必然 404 失败（见 notes 那段的注释）。
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
    #
    # **2026-11-26**：失败的主图也要摘掉。主图会被 `select_translatable_images`
    # 挑出来送去翻译，留着死链同样会在 `_fetch_source` 里 404。
    # 主图列表直接就是 URL 数组，删元素即可，不会留下 HTML 残片。
    main = payload.get("imgUrls")
    if isinstance(main, list) and main:
        kept_main: list[Any] = []
        for url in main:
            final, status = store.persist(url)
            if status == "stored":
                report["stored"] += 1
                report["main"] += 1
            elif status == "failed":
                report["failed"] += 1
                report["droppedMain"] = int(report.get("droppedMain") or 0) + 1
                continue  # 死链直接丢
            kept_main.append(final)
        payload["imgUrls"] = kept_main

    # 详情图（notes 是 HTML 片段，只替换其中的图片地址）
    #
    # **2026-11-26 修**：下载失败的详情图必须从 notes 里删掉。
    #
    # 现场（detailId=3563411810，负责人截图「为什么又有失败的」）：
    #
    #     文字处理失败：下载原图失败：HTTP 404
    #
    # 详情 HTML 里混了一张 1688 图床的死链
    # （`cbu01.alicdn.com/...O1CN01xl5x8O26Sn9YyfviO_!!2216730257661-0-cib.jpg`
    # 加 .jpg/.png/.webp 全是 404）。这里只记了个 failed 就把原地址留在
    # notes 里，后面图片翻译阶段按 notes 里的 URL 去下载，404 抛错，
    # 整件商品处理失败。
    #
    # 死链留在 payload 里没有任何价值：它本来就打不开，妙手侧也显示不出来，
    # 还一定会把图片翻译卡死。直接剔除，其余正常的图照常转存。
    notes = payload.get("notes")
    if isinstance(notes, str) and notes:
        detail_urls = pre_save_check.extract_detail_image_urls(notes)
        if detail_urls:
            new_notes = notes
            stored = 0
            dropped = 0
            for old in dict.fromkeys(detail_urls):
                final, status = store.persist(old)
                if status == "stored" and final != old:
                    new_notes = new_notes.replace(old, final)
                    stored += 1
                elif status == "failed":
                    report["failed"] += 1
                    # 彻底删掉这个 <img>（含可能残缺的结尾），不留死链
                    new_notes, hits = _drop_dead_detail_image(new_notes, old)
                    dropped += hits
            if stored or dropped:
                payload["notes"] = new_notes
                report["detail"] = stored
                report["stored"] += stored
                if dropped:
                    report["dropped"] = dropped

    # SKU 图
    #
    # **2026-11-26**：死链要清空 `imgUrl`，但**不能删颜色值**。
    # 删值会让 skuMap 里的键失去对应属性定义，保存时妙手必然报
    # 「SKU 不存在」；而留着死链，图片翻译按它去下载一样 404。
    #
    # 清空后要把清空的槽位记进 `_droppedSkuImages`（本地字段，不发给妙手），
    # 让读回校验知道「这几个位置的图是我们主动不要的」，否则回读比对
    # 「原来有图、现在没有」会判成不一致，把正常商品挡在入库外。
    try:
        sku_sources = warehouse_service.sku_image_sources(payload)
    except Exception:  # noqa: BLE001
        sku_sources = []
    if sku_sources:
        replacements: dict[str, str] = {}
        cleared: set[str] = set()
        for source in sku_sources:
            old = str(source.get("url") or "")
            final, status = store.persist(old)
            if status == "stored" and final != old:
                replacements[old] = final
                report["sku"] += 1
                report["stored"] += 1
            elif status == "failed":
                report["failed"] += 1
                replacements[old] = ""
                cleared.add(old)
                report["droppedSku"] = int(report.get("droppedSku") or 0) + 1
        if replacements:
            try:
                payload["skuPropertyList"] = warehouse_service.rewrite_sku_image_urls(
                    payload.get("skuPropertyList"), replacements)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SKU 图地址替换失败：%s", exc)
        if cleared:
            # 记录被清空的槽位（属性名|值名），供 readback_check 放行
            dead_slots: list[str] = []
            for prop in (payload.get("skuPropertyList") or []):
                if not isinstance(prop, dict):
                    continue
                name = str(prop.get("attrNameCn") or prop.get("attrName")
                           or prop.get("propName") or "").strip()
                for value in (prop.get("attrValueList") or []):
                    if not isinstance(value, dict):
                        continue
                    label = str(value.get("attrValue") or "").strip()
                    if label:
                        dead_slots.append(f"{name}|{label}")
            # 只记「现在确实没有图」的槽位
            payload["_droppedSkuImages"] = dead_slots

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
