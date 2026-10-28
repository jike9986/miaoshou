"""更新包下载 + 校验 + 安装。

老版 `tk-product-automation/src/update_check.py` 里 `download_and_install()` 才是
真正干活的那个：下载到临时目录、边下边算 sha256、校验不符就删掉、然后交给 Inno
静默安装。新版重构时只搬了「清单比对」，下载那半截丢了，`/system/update` 便永远
返回 `downloaded: False`——更新按钮点下去什么也不会发生。

这里把下载/校验/安装补回来，并且保留两条硬约束：

1. **只下 https、只下公网**。更新包地址最终来自清单（远端 JSON），一旦被改写就是
   一台能替服务端发请求的机器，所以逐条校验 scheme / 端口 / 解析出的 IP 段；
   重定向也要逐跳校验（httpx 自己跟跳转会绕过校验）。
2. **先写 .part 再原子 rename**。安装包动辄几十兆，直接写目标文件时断电/崩溃会留下
   半截 exe，下一次又被当成「已下载」交给安装器；先落临时文件、校验通过再 replace，
   失败路径一律把临时文件清掉。
"""
from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from core import errors
from core.config import get_settings
from core.constants import APP_VERSION

# 单跳连接 / 整体下载的超时：安装包在移动网络下也别轻易判死
DOWNLOAD_TIMEOUT = 900.0
CONNECT_TIMEOUT = 15.0
CHUNK_SIZE = 262144
MAX_REDIRECTS = 3

# 明确拒绝的主机名（内网、回环、云元数据端点）
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "169.254.169.254", "0.0.0.0"}
# RFC 2544 benchmark 网段：本机代理（Clash / TUN 模式）会把公网域名解析到这里。
# 与 services/image_review_service._is_private_host 保持同一套例外，否则开着代理的
# 机器永远下不了更新包；该网段由代理转发，不指向内网服务。
_BENCHMARK_NET = ipaddress.ip_network("198.18.0.0/15")


def _is_private_host(host: str) -> bool:
    """拒绝私有 / 回环 / 链路本地地址，避免更新包地址变成 SSRF 探针。

    解析失败不算内网：域名不存在时 httpx 自己也连不上，不必在这里提前判死。
    """
    if host in BLOCKED_HOSTS:
        return True
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version == 4 and ip in _BENCHMARK_NET:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def validate_download_url(url: str, *, host_whitelist: set[str] | None = None) -> str:
    """校验更新包地址，返回原样 URL（校验不过直接抛 AppError）。

    `host_whitelist` 为空表示不套域名白名单——调用方只在「地址来自外部请求体」时传，
    清单里给出的地址由清单域本身背书。
    """
    text = str(url or "").strip()
    if not text:
        raise errors.validation("更新包地址不能为空", field="url")
    parts = urlsplit(text)
    if parts.scheme != "https":
        raise errors.validation("更新包地址必须是 https", field="url", value=text)
    if parts.username or parts.password:
        raise errors.validation("更新包地址不能带账号密码", field="url")
    host = (parts.hostname or "").lower()
    if not host:
        raise errors.validation("更新包地址缺少域名", field="url", value=text)
    if parts.port not in (None, 443):
        raise errors.AppError(
            errors.FORBIDDEN_ORIGIN,
            f"更新包端口 {parts.port} 不允许（只允许 443）",
            status=403, details={"host": host, "port": parts.port},
        )
    if host_whitelist and host not in host_whitelist:
        raise errors.AppError(
            errors.FORBIDDEN_ORIGIN,
            f"更新包域名 {host} 不在白名单",
            status=403, details={"host": host, "whitelist": sorted(host_whitelist)},
        )
    if _is_private_host(host):
        raise errors.AppError(
            errors.FORBIDDEN_ORIGIN,
            f"更新包域名 {host} 指向内网/回环地址，已拒绝",
            status=403, details={"host": host},
        )
    return text


def _filename_for(url: str) -> str:
    """从 URL 取文件名，只保留安全字符——防止 `../` 把安装包写到数据目录外。"""
    raw = Path(urlsplit(url).path).name
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in "._-").lstrip(".")
    stem = Path(cleaned).stem[:100] or "setup"
    suffix = Path(cleaned).suffix.lower() or ".exe"
    return f"{stem}{suffix}"


def updates_dir() -> Path:
    return get_settings().data_dir / "updates"


def can_install() -> bool:
    """能不能替用户装：只有 Windows 桌面端有静默安装的入口。"""
    return sys.platform == "win32"


@contextlib.contextmanager
def _stream(url: str, *, host_whitelist: set[str] | None) -> Iterator[httpx.Response]:
    """打开下载流，手动跟随重定向并逐跳校验。

    httpx 的 `follow_redirects=True` 会在内部跟完跳转才把最终响应交给我们，
    中途的跳转目标一次都没经过校验——302 到 127.0.0.1 就绕过了 SSRF 防护。
    这里自己循环，每一跳都重新做 https + 内网校验（域名白名单只查首跳，
    正常做法是首跳在自家域、实际文件落 CDN）。
    """
    headers = {"User-Agent": f"AOHUANG-Listing/{APP_VERSION}",
               "Accept": "application/octet-stream,*/*"}
    timeout = httpx.Timeout(DOWNLOAD_TIMEOUT, connect=CONNECT_TIMEOUT)
    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for hop in range(MAX_REDIRECTS + 1):
            validate_download_url(current, host_whitelist=host_whitelist if hop == 0 else None)
            request = client.build_request("GET", current, headers=headers)
            response = client.send(request, stream=True)
            if response.is_redirect:
                location = response.headers.get("location") or ""
                response.close()
                if not location:
                    raise errors.AppError(errors.UPSTREAM_ERROR,
                                          "更新包地址返回了空跳转", status=502)
                current = str(httpx.URL(current).join(location))
                continue
            if response.status_code != 200:
                status = response.status_code
                response.close()
                raise errors.AppError(
                    errors.UPSTREAM_ERROR,
                    f"下载更新包失败（HTTP {status}）", status=502,
                    details={"url": current, "status": status})
            try:
                yield response
            finally:
                response.close()
            return
    raise errors.AppError(errors.UPSTREAM_ERROR,
                          f"更新包地址重定向超过 {MAX_REDIRECTS} 次，已放弃", status=502)


def download_and_install(
    url: str,
    sha256: str = "",
    *,
    host_whitelist: set[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """下载更新包到 `data_dir/updates/`，校验后原子落盘；安装交给 `install()`。

    函数名跟老版保持一致，但**不再自动执行安装包**：新版由接口决定什么时候装
    （用户点「安装」或在共享模式下交给宿主），下载与安装分开才好回滚、好排查。

    返回 `{downloaded, path, size, sha256, sha256Verified, canInstall}`：
    上游给了 sha256 时 `sha256Verified=True`；没给就本地算一个回填，明确标记未校验。
    """
    safe_url = validate_download_url(url, host_whitelist=host_whitelist)
    expected = str(sha256 or "").strip().lower()
    if expected and (len(expected) != 64
                     or any(ch not in "0123456789abcdef" for ch in expected)):
        raise errors.validation("sha256 必须是 64 位十六进制", field="sha256", value=sha256)

    target_dir = updates_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    final = target_dir / _filename_for(safe_url)
    part = final.with_name(final.name + ".part")

    digest = hashlib.sha256()
    written = 0
    try:
        with _stream(safe_url, host_whitelist=host_whitelist) as response:
            total = int(response.headers.get("Content-Length") or 0)
            with part.open("wb") as stream:
                for chunk in response.iter_bytes(CHUNK_SIZE):
                    if not chunk:
                        continue
                    stream.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if progress is not None and total:
                        progress(written, total)
                # 必须 fsync：断电后 rename 成功但内容还在页缓存里，就会拿到截断的 exe
                stream.flush()
                os.fsync(stream.fileno())
    except BaseException:
        # 任何中断（超时、用户取消、磁盘满）都不留半截文件
        part.unlink(missing_ok=True)
        raise

    actual = digest.hexdigest()
    if written <= 0:
        part.unlink(missing_ok=True)
        raise errors.AppError(errors.UPSTREAM_ERROR, "更新包为空，已删除", status=502)
    if expected and actual != expected:
        # 校验失败的文件绝不能留：留着就可能被下一次「已下载」直接交给安装器
        part.unlink(missing_ok=True)
        raise errors.AppError(
            errors.DIGEST_MISMATCH, "安装包校验失败，已删除", status=422,
            details={"expected": expected, "actual": actual, "size": written})
    os.replace(part, final)
    return {
        "downloaded": True,
        "path": str(final),
        "size": written,
        "sha256": actual,
        # 上游没给哈希时这个文件没法证明来源，标记未校验，别让调用方以为验过了
        "sha256Verified": bool(expected),
        "canInstall": can_install(),
    }


def last_downloaded() -> str:
    """最近一次下载成功的安装包路径；没有就返回空串。"""
    directory = updates_dir()
    if not directory.is_dir():
        return ""
    candidates = [p for p in directory.iterdir()
                  if p.is_file() and p.suffix.lower() in {".exe", ".msi"}]
    if not candidates:
        return ""
    try:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    except OSError:
        return ""


def install(path: str | Path, *, silent: bool = True) -> dict[str, Any]:
    """运行安装包：Windows 上用 `/S` 静默安装，静默拉起失败再退回系统默认关联打开。

    非 Windows 明确报错，而不是假装成功——桌面端只有 Windows 构建。
    """
    target = Path(str(path))
    if not target.is_file():
        raise errors.not_found(f"安装包不存在：{target}", path=str(target))
    if not can_install():
        raise errors.AppError(
            "UNSUPPORTED_PLATFORM",
            "当前系统不支持静默安装（仅 Windows 桌面端可用）",
            status=400, details={"platform": sys.platform})
    if silent:
        try:
            # 独立进程 + DEVNULL：服务退出时不把安装器一起带走，也不占管道
            subprocess.Popen([str(target), "/S"],
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             close_fds=True)
            return {"ok": True, "path": str(target), "silent": True}
        except OSError:
            # 安装包不认 /S（例如 NSIS 之外的封装）时退回 startfile，至少能弹向导
            pass
    try:
        os.startfile(str(target))  # type: ignore[attr-defined]  # 仅 Windows 存在
    except OSError as exc:
        raise errors.AppError(errors.INTERNAL_ERROR,
                              f"启动安装包失败：{exc}", status=500,
                              details={"path": str(target)}) from None
    return {"ok": True, "path": str(target), "silent": False}
