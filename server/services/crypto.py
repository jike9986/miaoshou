"""凭据加解密。

老版（`credential_store.py`）在 Windows 上用系统凭据库（Credential Manager /
DPAPI 体系）保存 AppSecret，不需要额外依赖，也不存在「密钥文件丢了就全废」的
问题。新版改成 Fernet + `secret.key`，带来两个真实故障：

  1. **`secret.key` 丢失或被重建**（换机器、重装、误删）→ 所有账号的
     AppSecret 全部解不出来。旧实现 `except InvalidToken: return ""` 把这件事
     咽下去，表现为「账号密码为空 → 登录失败」，用户完全看不出真正原因，
     而且**任何一次成功的保存都会把空值写回去，把密文彻底冲掉**。
  2. 写文件不是原子的，中途断电会留下半截 key 或半截密文。

现在的策略：
  · Windows 上优先走 DPAPI（`CryptProtectData` / `CryptUnprotectData`，
    绑定当前用户账户），没有 DPAPI 时退回 Fernet；
  · 解不开时**抛错**，绝不返回空串——让调用方明确报「凭据已损坏」；
  · `_write_file` 先写临时文件再原子替换，并按 0600 收紧权限。

DPAPI 密文格式：`dpapi:` 前缀 + base64。带前缀才能和历史 Fernet 密文共存，
读取时按前缀分流，老数据不用迁移。
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from core.config import get_settings

DPAPI_PREFIX = "dpapi:"
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


class CredentialError(ValueError):
    """凭据无法解密。宁可报错，也不要静默返回空串。"""


# --- DPAPI（Windows 原生，绑定当前用户） -----------------------------------
def _dpapi():
    """返回 (CryptProtectData, CryptUnprotectData, ctypes, wintypes)，不可用则 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    except (AttributeError, OSError):
        return None
    return crypt32, kernel32, ctypes, wintypes, DATA_BLOB


def _blob(ctypes, DATA_BLOB, payload: bytes):
    buffer = ctypes.create_string_buffer(payload, len(payload))
    return DATA_BLOB(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi_encrypt(plain: str) -> str | None:
    api = _dpapi()
    if api is None:
        return None
    crypt32, kernel32, ctypes, wintypes, DATA_BLOB = api
    source, keep = _blob(ctypes, DATA_BLOB, plain.encode("utf-8"))
    target = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(source), "aohuang",
                                    None, None, None, 0, ctypes.byref(target)):
        return None
    try:
        raw = ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))
    return DPAPI_PREFIX + base64.b64encode(raw).decode("ascii")


def _dpapi_decrypt(token: str) -> str | None:
    api = _dpapi()
    if api is None:
        return None
    crypt32, kernel32, ctypes, wintypes, DATA_BLOB = api
    try:
        raw = base64.b64decode(token[len(DPAPI_PREFIX):], validate=True)
    except (ValueError, TypeError):
        raise CredentialError("凭据内容不是合法的 base64") from None
    source, keep = _blob(ctypes, DATA_BLOB, raw)
    target = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None,
                                     None, None, 0, ctypes.byref(target)):
        raise CredentialError(
            "凭据无法用当前 Windows 账户解密：可能换过登录账户，或凭据文件被复制到了别的机器")
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


# --- Fernet 兜底 -----------------------------------------------------------
def _fernet() -> Fernet:
    secret = get_settings().local_secret().encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def _write_file(path: Path, payload: bytes) -> None:
    """原子写 + 0600：先落临时文件，再 replace。

    直接覆盖写会在断电/崩溃时留下半截文件，而 `secret.key` 半截就等于
    所有账号密文永久失联。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Windows 上文件被打开时 os.replace 会失败，所以 with 结束（句柄关闭）后才替换。
        with tempfile.NamedTemporaryFile(dir=str(path.parent), delete=False,
                                         prefix=path.name, suffix=".tmp") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # chmod 失败（不支持权限位的文件系统）不该让整个写入失败
        with contextlib.suppress(OSError):
            os.chmod(handle.name, _FILE_MODE)
        os.replace(handle.name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise


# --- 公共 API --------------------------------------------------------------
def encrypt(plain: str) -> str:
    if not plain:
        return ""
    protected = _dpapi_encrypt(plain)
    if protected is not None:
        return protected
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """解出明文；解不开时抛 `CredentialError`，**不再返回空串**。

    返回空串会让上层以为「这个账号没设密码」，进而在下一次保存时把空值
    写回数据库，把还能救的密文冲掉。
    """
    if not token:
        return ""
    if token.startswith(DPAPI_PREFIX):
        restored = _dpapi_decrypt(token)
        if restored is not None:
            return restored
        raise CredentialError("凭据是 DPAPI 格式，但当前环境没有 DPAPI 可用")
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        raise CredentialError(
            "凭据无法解密：local secret 已变更或凭据文件损坏，请重新填写 AppSecret"
        ) from None


def try_decrypt(token: str) -> str:
    """只在「读出来展示/校验」这类可容忍失败的场合用；写回数据库前必须用 decrypt。"""
    try:
        return decrypt(token)
    except CredentialError:
        return ""


def hints(app_key: str, app_secret: str) -> tuple[str, str]:
    """(appIdHint, secretHint)：只暴露末 4 位，界面用它们区分账号。"""
    key = (app_key or "").strip()
    secret = (app_secret or "").strip()
    key_hint = f"…{key[-4:]}" if len(key) > 4 else ("…" if key else "")
    secret_hint = f"…{secret[-4:]}" if len(secret) > 4 else ("…" if secret else "")
    return key_hint, secret_hint
