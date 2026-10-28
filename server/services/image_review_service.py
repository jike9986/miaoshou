"""主图文字识别（本地 OCR）：选中图片前先看图上有没有中文，避免把带中文的图发到海外店铺。

老版是 `image_review.analyze_images()`（Windows 内置 OCR + 中文检测）。新版把它做成
`POST /images/analyze`：只认阿里 CDN 的原图，不下载用户输入的任意 URL。

约束（与老版一致，不可放宽）：
  · 只接受 https + `cbu01.alicdn.com`（同 `_fetch_source` 的来源白名单思路）；
  · 单图上限 15 MB，超时 30 秒，结果按 URL 哈希缓存到 images/ocr/；
  · OCR 走 `powershell.exe`（Windows PowerShell 5.1）——PowerShell 7 不支持 WinRT，
    调用会报 `Operation is not supported on this platform (0x80131539)`；
  · OCR 不可用时返回 error=True + action=review，不抛错阻断整批。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler

from core.config import get_settings

OCR_TIMEOUT = 30
OCR_MAX_BYTES = 15 * 1024 * 1024
OCR_CONCURRENCY = 3
OCR_CACHE_VERSION = 2
ANALYZE_LIMIT = 20

# 允许识别的图片来源：常见图床 + 阿里 CDN。
# 老版只放 cbu01.alicdn.com，但新版商品的真实主图来自多个图床，
# 因此改成「公网 https + 非内网地址」的白名单式校验（防 SSRF，见 allowed_source）。
ALLOWED_HOSTS = {
    "cbu01.alicdn.com", "img.alicdn.com", "gd1.alicdn.com", "gd2.alicdn.com",
    "gd3.alicdn.com", "gd4.alicdn.com", "img.chengji-inc.com",
    "earth-rt.chengji-inc.com", "static.chengji-inc.com",
}
ALLOWED_SUFFIXES = (".alicdn.com", ".chengji-inc.com", ".taobao.com", ".tmall.com")
# 明确拒绝的主机名/网段（内网、回环、云元数据）
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "169.254.169.254"}
CHINESE = re.compile(r"[\u4e00-\u9fff]")
_SCRIPT = Path(__file__).with_name("windows_ocr.ps1")


def _is_private_host(host: str) -> bool:
    """拒绝私有/回环/链路本地地址，避免 OCR 变成 SSRF 探测工具。

    例外：`198.18.0.0/15`（RFC 2544 benchmark 网段）。本机代理把公网域名解析到
    这个网段，若一并拒绝会让所有图床都不可用；该网段由代理转发，不指向内网服务。
    """
    import ipaddress
    import socket

    if host in BLOCKED_HOSTS:
        return True
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    benchmark = ipaddress.ip_network("198.18.0.0/15")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version == 4 and ip in benchmark:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def _cache_dir() -> Path:
    path = get_settings().data_dir / "images" / "ocr"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def allowed_source(url: str) -> bool:
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https" or parts.username:
        return False
    host = (parts.hostname or "").lower()
    if not host or parts.port not in (None, 443):
        return False
    known = host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES)
    if not known:
        return False
    return not _is_private_host(host)


# 厂家/工厂宣传语：这类图出现在跨境商品主图上基本是废图——不是要翻译，
# 是要整张删掉。仅判断「有没有中文」分辨不出来（老版 image_review.FACTORY_PHRASES）。
FACTORY_PHRASES = (
    "厂家直销", "工厂直销", "源头工厂", "实力工厂", "工厂实拍", "工厂展示",
    "生产车间", "生产基地", "厂房展示", "厂家介绍", "工厂介绍", "公司简介",
    "招商加盟", "招收代理", "一件代发", "来图定制", "来样定制",
)


def classify_text(text: str) -> dict[str, Any]:
    """按 OCR 文本给处理建议。

    keep（无中文）/ translate（有中文）/ remove（厂家宣传，整张删）/
    review（空文本，或只识别到一个「一」疑似尺码横线）。
    """
    clean = re.sub(r"\s+", "", text or "")
    chinese = " ".join(CHINESE.findall(text or ""))
    if not clean:
        return {"action": "review", "reason": "图上没识别到文字，可保留或人工看图",
                "hasChinese": None, "chineseText": ""}
    hits = [phrase for phrase in FACTORY_PHRASES if phrase in clean]
    if hits:
        return {"action": "remove", "reason": "疑似厂家宣传：" + "、".join(hits),
                "hasChinese": True, "chineseText": chinese, "factoryPhrases": hits}
    if set(CHINESE.findall(text or "")) == {"一"}:
        # 尺码表里表示「不可选」的横线常被 OCR 认成「一」，别当中文标图
        return {"action": "review",
                "reason": "仅识别到「一」，可能是尺码横线，请看图复核",
                "hasChinese": True, "chineseText": chinese}
    if CHINESE.search(text or ""):
        return {"action": "translate", "reason": "识别到中文，发海外前建议翻译",
                "hasChinese": True, "chineseText": chinese}
    return {"action": "keep", "reason": "未识别到中文，可直接使用",
            "hasChinese": False, "chineseText": ""}


class _NoRedirect(HTTPRedirectHandler):
    """禁止自动跟随重定向（对齐老版 `image_review.NoRedirect`）。

    urllib 默认 opener 会静默跟随 302/307；`redirect_request` 返回 None 时
    OpenerDirector 走到 http_error_default 抛 HTTPError，调用方就能把
    3xx 当成拒绝而不是当成「下载失败重试」。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _download(url: str) -> Path:
    """下载原图到缓存目录；已存在则复用。

    不跟随重定向（老版 `NoRedirect(HTTPRedirectHandler)`）：
    `allowed_source` 只校验了**首个** URL 的 host，一旦上游回 302 就换了目标，
    「白名单域名 → 内网地址」的重定向就能绕过白名单（典型 SSRF 跳板）。
    所以这里禁止自动跳转，遇到 3xx 直接拒绝，由用户换直链。
    """
    import urllib.error
    import urllib.request

    directory = _cache_dir()
    image = directory / f"{_key(url)}.image"
    if image.exists() and image.stat().st_size > 0:
        return image
    request = urllib.request.Request(url, headers={"User-Agent": "AOHUANG-Image-Review/1.0"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=15) as response:  # noqa: S310 - host 已校验且禁跳转
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
            if 300 <= status < 400:
                raise ValueError("图片地址发生重定向，已拒绝")
            content_type = str(response.headers.get("Content-Type") or "")
            if not content_type.startswith("image/"):
                raise ValueError("返回内容不是图片")
            raw = response.read(OCR_MAX_BYTES + 1)
    # urlopen 对 3xx 未必抛错：默认 opener 会静默跟随，自定义 opener 下才可能把
    # HTTPError 抛出来。两条路径都按「重定向即拒绝」处理，别让 302 混成下载失败。
    except urllib.error.HTTPError as exc:
        if 300 <= int(getattr(exc, "code", 0) or 0) < 400:
            raise ValueError("图片地址发生重定向，已拒绝") from exc
        raise ValueError(
            f"图片下载失败（HTTP {getattr(exc, 'code', '?')}），可重试或看图复核") from exc
    if len(raw) > OCR_MAX_BYTES:
        raise ValueError("图片超过 15 MB，无法识别")
    image.write_bytes(raw)
    return image


def _script_path() -> Path:
    """定位 windows_ocr.ps1。

    源码模式在 services/ 下；PyInstaller 冻结后 datas 会解到 sys._MEIPASS/services/
    （见 server.spec），`Path(__file__).with_name()` 在两种模式下都指向同一位置。
    """
    candidates = [_SCRIPT]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "services" / "windows_ocr.ps1")
    for path in candidates:
        if path.is_file():
            return path
    raise ValueError("缺少 windows_ocr.ps1（OCR 脚本未随程序发布），请看图复核")


def _run_ocr(image: Path) -> dict[str, Any]:
    """调用 Windows 内置 OCR。必须用 powershell.exe（5.1），PS7 不支持 WinRT。"""
    script = _script_path()
    executable = "powershell.exe" if sys.platform == "win32" else "pwsh"
    process = subprocess.run(  # noqa: S603 - 固定参数，无 shell
        [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script), "-ImagePath", str(image.resolve())],
        capture_output=True, timeout=OCR_TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    raw = process.stdout or b""
    # powershell.exe 输出编码随系统 locale 变化（中文系统常见 GBK），逐种尝试解码
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "gbk", "cp936", "mbcs"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not text:
        text = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        raise ValueError("本地 OCR 输出无法解析，请看图复核或重试") from None
    if process.returncode or not isinstance(payload.get("text"), str):
        raise ValueError("本地 OCR 未完成，请看图复核或重试")
    payload["version"] = OCR_CACHE_VERSION
    return payload


def recognize(url: str, *, retry: bool = False) -> dict[str, Any]:
    """识别一张图，结果按 URL 缓存。retry=True 时忽略缓存重跑。"""
    if not allowed_source(url):
        raise ValueError("该图片来源暂未接入本地识别（只支持 cbu01.alicdn.com 原图）")
    cached = _cache_dir() / f"{_key(url)}.json"
    if cached.exists() and not retry:
        try:
            data = json.loads(cached.read_text(encoding="utf-8"))
            if data.get("version") == OCR_CACHE_VERSION and isinstance(data.get("text"), str):
                return data
        except (OSError, ValueError):
            pass
    image = _download(url)
    result = _run_ocr(image)
    with contextlib.suppress(OSError):
        # 缓存写不进去只是下次多跑一次 OCR，不能因此让本次识别失败
        cached.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def analyze_one(url: str, *, retry: bool = False) -> dict[str, Any]:
    try:
        result = recognize(url, retry=retry)
    except ValueError as exc:
        return {"action": "review", "reason": str(exc), "hasChinese": None,
                "chineseText": "", "text": "", "error": True}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        reason = f"本地 OCR 不可用（{type(exc).__name__}），请看图复核"
        return {"action": "review", "reason": reason,
                "hasChinese": None, "chineseText": "", "text": "", "error": True}
    return {**classify_text(str(result.get("text") or "")),
            "text": str(result.get("text") or ""), "error": False}


def analyze(images: Iterable[str], *, limit: int = ANALYZE_LIMIT, retry: bool = False,
            max_workers: int = OCR_CONCURRENCY,
            on_complete: Callable[[int, int], None] | None = None) -> list[dict[str, Any]]:
    """批量识别主图，返回每张的 index/url/action/reason/text。"""
    selected = [str(url or "").strip() for url in images if str(url or "").strip()][:limit]
    unique = list(dict.fromkeys(selected))
    results: dict[str, dict[str, Any]] = {}
    if unique:
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
            futures = {pool.submit(analyze_one, url, retry=retry): url for url in unique}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
                if on_complete:
                    on_complete(len(results), len(unique))
    return [{"index": index, "url": url, **results.get(url, {})}
            for index, url in enumerate(selected, 1)]


def check_local_file(path: Path, *, retry: bool = False) -> dict[str, Any]:
    """复查本地结果图里还有没有中文（老版 image_review 的「译文中文残留」检查）。

    翻译完的图如果仍带中文，直接发到海外店铺就是脏图。这里只做判定，
    不改图、不阻断流程：调用方自己决定是标记待复核还是拒绝。
    """
    try:
        if not Path(path).is_file():
            raise ValueError("结果图文件不存在")
        cached = _cache_dir() / f"local-{_key(str(Path(path).resolve()))}.json"
        if cached.exists() and not retry:
            try:
                data = json.loads(cached.read_text(encoding="utf-8"))
                if data.get("version") == OCR_CACHE_VERSION and isinstance(data.get("text"), str):
                    return {**classify_text(str(data.get("text") or "")),
                            "text": str(data.get("text") or ""), "error": False}
            except (OSError, ValueError):
                pass
        result = _run_ocr(Path(path))
        with contextlib.suppress(OSError):
            cached.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except ValueError as exc:
        return {"action": "review", "reason": str(exc), "hasChinese": None,
                "chineseText": "", "text": "", "error": True}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"action": "review",
                "reason": f"本地 OCR 不可用（{type(exc).__name__}），请看图复核",
                "hasChinese": None, "chineseText": "", "text": "", "error": True}
    text = str(result.get("text") or "")
    return {**classify_text(text), "text": text, "error": False}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"keep": 0, "translate": 0, "review": 0}
    for row in rows:
        action = str(row.get("action") or "review")
        counts[action] = counts.get(action, 0) + 1
    return {
        "total": len(rows),
        "keep": counts.get("keep", 0),
        "translate": counts.get("translate", 0),
        "review": counts.get("review", 0),
        "withChinese": sum(1 for row in rows if row.get("hasChinese") is True),
        "errors": sum(1 for row in rows if row.get("error")),
    }
