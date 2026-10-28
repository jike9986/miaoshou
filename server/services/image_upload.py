"""把本地图片传到公开 HTTPS 图床（移植自老项目 src/image_upload.py）。

老版 `POST /api/upload-size-chart` 的真实链路是：
    读字节 → 校验类型 → 存临时文件 → upload_image() → 得到图床 URL → 记成书签
新版之前只做了最后一步「登记 URL」，所以界面上只能贴地址、不能选文件。
这里把上传补上，口径与老版一致：

  · 优先走 IMAGE_UPLOAD_URL（HTTP multipart），未配置则走 SFTP
  · SFTP 默认 host / 远程目录 / 公开前缀与老版完全相同，指向同一个图床
  · 凭据从环境变量读，缺省回落到桌面「雨云.txt」前两行（用户名 / 密码）
  · 任何异常都只报中文短语，绝不把凭据写进异常或日志

安全边界（沿用老版）：
  · 远程目录必须以 / 开头且不含 ..
  · 公开前缀必须是 HTTPS、不含账号密码、以 / 结尾
  · 返回的 URL 不接受 localhost / 内网地址
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import socket
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

__all__ = ["ImageUploadError", "upload_image", "image_type", "SIZE_CHART_UPLOAD_LIMIT"]


class ImageUploadError(ValueError):
    """上传失败。message 是可以直接展示给用户的中文短语。"""


# 尺码图上限 8 MB（老版 SIZE_CHART_UPLOAD_LIMIT）
SIZE_CHART_UPLOAD_LIMIT = 8 * 1024 * 1024

DEFAULT_SFTP_HOST = "38.95.75.47"
DEFAULT_SFTP_REMOTE_DIR = "/var/www/tk-product-automation/translation-temp"
DEFAULT_SFTP_PUBLIC_BASE = "https://aohuang888.com/tk-product-automation/translation-temp/"

def image_type(raw: bytes) -> tuple[str, str]:
    """按魔数判真实类型，返回 (mime, extension)。判不出就报错。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise ImageUploadError("请选择 PNG、JPEG 或 WebP 图片")


def _sftp_credentials() -> tuple[str, str, str, int]:
    """读 SFTP 凭据。异常里只给短语，不回显任何凭据内容。"""
    host = os.environ.get("IMAGE_SFTP_HOST", DEFAULT_SFTP_HOST).strip()
    user = os.environ.get("IMAGE_SFTP_USER", "").strip()
    password = os.environ.get("IMAGE_SFTP_PASSWORD", "").strip()
    credential_file = os.environ.get(
        "IMAGE_SFTP_CREDENTIAL_FILE",
        str(Path.home() / "Desktop" / "雨云.txt"),
    ).strip()
    if not user or not password:
        try:
            lines = [
                line.strip()
                for line in Path(credential_file).read_text(encoding="utf-8-sig").splitlines()
                if line.strip()
            ]
        except OSError as exc:
            raise ImageUploadError("图片服务器凭据文件无法读取") from exc
        if len(lines) >= 2:
            user = user or lines[0]
            password = password or lines[1]
        elif len(lines) == 1:
            password = password or lines[0]
    if not host or not user or not password:
        raise ImageUploadError("未配置图片服务器凭据")
    try:
        port = int(os.environ.get("IMAGE_SFTP_PORT", "22"))
    except ValueError as exc:
        raise ImageUploadError("图片服务器端口无效") from exc
    if not 1 <= port <= 65535:
        raise ImageUploadError("图片服务器端口无效")
    return host, user, password, port


def _sftp_remote_settings() -> tuple[str, str]:
    remote_dir = (os.environ.get("IMAGE_SFTP_REMOTE_DIR", DEFAULT_SFTP_REMOTE_DIR)
                  .strip().rstrip("/"))
    public_base = os.environ.get("IMAGE_SFTP_PUBLIC_BASE_URL", DEFAULT_SFTP_PUBLIC_BASE).strip()
    if not remote_dir.startswith("/") or ".." in remote_dir.split("/"):
        raise ImageUploadError("图片服务器远程目录无效")
    parts = urlsplit(public_base)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or not public_base.endswith("/")
    ):
        raise ImageUploadError("图片服务器公开地址必须是以斜杠结尾的 HTTPS 地址")
    return remote_dir, public_base


def _safe_filename(source: Path, raw: bytes | None = None) -> str:
    """文件名安全化：不合法就用内容 sha256 命名，扩展名限定在白名单里。"""
    filename = source.name
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename) and not filename.endswith("."):
        return filename
    digest = hashlib.sha256(raw if raw is not None else source.read_bytes()).hexdigest()
    extension = source.suffix.casefold()
    if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
        extension = ".bin"
    return digest + extension


def _public_url(value: object) -> str:
    if not isinstance(value, str):
        raise ImageUploadError("上传没有返回图片地址")
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ImageUploadError("图片地址必须是 HTTPS")
    host = parts.hostname.casefold().rstrip(".")
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        raise ImageUploadError("图片地址是无效的本机地址")
    try:
        addresses = socket.getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        addresses = []
    for address in addresses:
        ip = address[4][0]
        if ip.startswith(("10.", "192.168.", "127.")):
            raise ImageUploadError("图片地址是无效的局域网地址")
        if ip.startswith("172."):
            # 只把解析失败算掉，不能在 try 里 raise：
            # ImageUploadError 继承 ValueError，会被自己的 except 吞掉，
            # 172.16.0.0/12 这段私网就漏过去了（真实回归）。
            try:
                second = int(ip.split(".")[1])
            except (IndexError, ValueError):
                continue
            else:
                if 16 <= second <= 31:
                    raise ImageUploadError("图片地址是无效的局域网地址")
    return url


def _find_url(payload: object) -> str | None:
    if isinstance(payload, dict):
        for key in ("url", "imageUrl", "image_url", "downloadUrl"):
            if payload.get(key):
                return payload[key]
        for value in payload.values():
            found = _find_url(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_url(value)
            if found:
                return found
    return None


def _upload_http(source: Path, *, timeout: int = 30) -> str:
    """POST multipart 到 IMAGE_UPLOAD_URL，取回图片地址。"""
    import json

    endpoint = os.environ.get("IMAGE_UPLOAD_URL", "").strip()
    if not endpoint:
        raise ImageUploadError("未配置图片上传地址")
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ImageUploadError("图片上传地址必须是公开 HTTPS")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ImageUploadError("图片文件无法读取") from exc
    if not raw:
        raise ImageUploadError("图片文件为空")

    boundary = "----aohuang" + secrets.token_hex(12)
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{source.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = head + raw + tail

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    token_file = os.environ.get("IMAGE_UPLOAD_TOKEN_FILE", "").strip()
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            raise ImageUploadError("图片上传 token 文件无法读取") from exc
        if not token:
            raise ImageUploadError("图片上传 token 为空")
        headers["Authorization"] = f"Bearer {token}"

    request = Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        raise ImageUploadError("图片上传失败") from exc
    url = _find_url(payload)
    if not url:
        raise ImageUploadError("上传接口没有返回图片地址")
    return _public_url(url)


def _upload_sftp(source: Path, *, timeout: int = 30) -> str:
    """SFTP 上传：先写隐藏临时名，校验大小后再改名，避免半截文件被访问。"""
    try:
        import paramiko
    except ImportError as exc:
        raise ImageUploadError("图片上传组件缺失（paramiko）") from exc

    host, user, password, port = _sftp_credentials()
    remote_dir, public_base = _sftp_remote_settings()
    local_size = source.stat().st_size
    if local_size <= 0:
        raise ImageUploadError("图片文件为空")

    filename = _safe_filename(source)
    remote_path = remote_dir + "/" + filename
    temporary_path = remote_dir + "/." + filename + "." + secrets.token_hex(8) + ".uploading"

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sftp = None
    temporary_exists = False
    try:
        client.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        sftp = client.open_sftp()
        try:
            sftp.stat(remote_dir)
        except OSError as exc:
            raise ImageUploadError("图片服务器远程目录不存在") from exc
        sftp.put(str(source), temporary_path)
        temporary_exists = True
        if sftp.stat(temporary_path).st_size != local_size:
            raise ImageUploadError("图片服务器文件校验失败")
        try:
            existing_size = sftp.stat(remote_path).st_size
        except OSError:
            existing_size = None
        if existing_size == local_size:
            # 同名同大小：内容相同，直接丢弃临时文件
            sftp.remove(temporary_path)
            temporary_exists = False
        elif hasattr(sftp, "posix_rename"):
            sftp.posix_rename(temporary_path, remote_path)
            temporary_exists = False
        else:
            sftp.rename(temporary_path, remote_path)
            temporary_exists = False
    except ImageUploadError:
        raise
    except (OSError, paramiko.SSHException) as exc:
        raise ImageUploadError("图片服务器上传失败") from exc
    finally:
        if sftp is not None and temporary_exists:
            # 临时文件删不掉不影响上传结果，静默放过（原来就是 except: pass）
            with contextlib.suppress(OSError):
                sftp.remove(temporary_path)
        if sftp is not None:
            sftp.close()
        client.close()
    return _public_url(public_base + filename)


def upload_image(path: str | Path, *, timeout: int = 30) -> str:
    """把本地图片传到图床，返回公开 HTTPS 地址。"""
    source = Path(path)
    if not source.is_file():
        raise ImageUploadError("图片文件无法读取")
    if os.environ.get("IMAGE_UPLOAD_URL", "").strip():
        return _upload_http(source, timeout=timeout)
    return _upload_sftp(source, timeout=timeout)
