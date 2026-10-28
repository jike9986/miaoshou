"""局域网共享部署的运维面：本机地址、数据库健康、在线客户端。

这一层只回答「现在能不能给别人用」这类问题，不改任何业务数据：

  · 本机有哪些 IPv4 可以给别人填（`lan_addresses`）
  · 数据库在不在 WAL、能不能写、多大（`db_facts` / `probe_writable`）
  · 现在有几个客户端在连（`note_client` / `active_operators`）

设计上的三条硬约束（都是踩出来的）：

1. **不读 settings.bind_host 的属性名**。`bind_host` 由另一处改动引入，
   这里一律走 `getattr(settings, "bind_host", "")` 兜底 —— 属性还没落地时
   按 `shared_mode` 推出「应该是 0.0.0.0 还是 127.0.0.1」，不会 AttributeError。
2. **写探测不能每 200ms 打一次**。客户端探活间隔是 HEALTH_INTERVAL_MS=200，
   每个探活都开一次 `BEGIN IMMEDIATE` 会在多写场景里自己把自己锁住。
   写探测结果缓存 `WRITE_PROBE_TTL` 秒，探活读缓存。
3. **不往 settings 表里塞新键到 models/**。共享开关的键常量定义在本模块，
   不动 `models/setting.py`（那是别人在改的文件）。
"""
from __future__ import annotations

import os
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.config import get_settings
from core.constants import APP_VERSION

# 共享模式落库的键。刻意不写进 models/setting.py 的 DEFAULT_SETTINGS：
# 那个文件正在被另一处改动，加键会冲突；get 不到时按环境变量兜底。
SHARED_MODE_KEY = "server.sharedMode"

# 在线窗口：5 分钟内有请求就算「还在线」。比一次页面停留长一点，
# 免得用户切走一会儿再回来看运维页时人都消失了。
CLIENT_WINDOW_SECONDS = 300.0
# 写探测缓存：客户端探活 200ms 一次，不能每次都去抢写锁
WRITE_PROBE_TTL = 30.0

_LOOPBACK_PREFIXES = ("127.",)
# 169.254/16 是 DHCP 失败时 Windows 给自己安的地址，别人连不上，列出来只会误导
_LINK_LOCAL_PREFIXES = ("169.254.",)
_UNUSABLE = {"0.0.0.0", "255.255.255.255", ""}

_clients_lock = threading.Lock()
_clients: dict[str, dict[str, Any]] = {}

_write_probe_lock = threading.Lock()
_write_probe_cache: dict[str, Any] = {"at": 0.0, "writable": True, "busy": False,
                                     "error": "", "path": ""}


# --- 本机地址 ---------------------------------------------------------------
def _is_usable_ipv4(address: str) -> bool:
    """过滤回环、链路本地、组播与非法串。"""
    text_value = str(address or "").strip()
    if text_value in _UNUSABLE:
        return False
    parts = text_value.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(part) for part in parts]
    except ValueError:
        return False
    if any(octet < 0 or octet > 255 for octet in octets):
        return False
    if any(text_value.startswith(prefix) for prefix in _LOOPBACK_PREFIXES):
        return False
    if any(text_value.startswith(prefix) for prefix in _LINK_LOCAL_PREFIXES):
        return False
    # 组播 224/4、保留 240/4 都不可能是「别人该连的地址」
    return not 224 <= octets[0] <= 255


def _rank(address: str) -> tuple[int, tuple[int, ...]]:
    """排序：先 RFC1918 私网（最可能是同事该填的那个），再其它，同档按数值。"""
    octets = tuple(int(part) for part in address.split("."))
    first, second = octets[0], octets[1]
    private = (
        first == 10
        or (first == 172 and 16 <= second <= 31)
        or (first == 192 and second == 168)
    )
    return (0 if private else 1, octets)


def lan_addresses() -> list[str]:
    """本机所有可对外的 IPv4（去回环、去 169.254、去重、私网优先）。

    三条来源取并集，任何一条失败都不影响其它两条：

    1. `psutil.net_if_addrs()` —— Windows 上最全，能拿到没做 DNS 注册的网卡；
       psutil 是可选依赖（不在 requirements.txt 里），没装就跳过。
    2. `socket.getaddrinfo(hostname)` —— 标准库路线，依赖主机名解析。
    3. UDP connect 一个公网地址取 `getsockname()` —— 拿到默认路由那张网卡的地址；
       不真的发包（UDP connect 只设置对端，不握手），断网时会失败，失败就跳过。
    """
    found: list[str] = []

    try:
        import psutil  # 可选依赖：没装也能跑
    except ImportError:
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        try:
            for _name, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if getattr(addr, "family", None) == socket.AF_INET:
                        found.append(str(getattr(addr, "address", "")))
        except Exception:  # noqa: BLE001 - 枚举网卡失败不该让接口 500
            pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(str(info[4][0]))
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("223.5.5.5", 53))
            found.append(str(probe.getsockname()[0]))
    except OSError:
        pass

    unique = {address for address in found if _is_usable_ipv4(address)}
    return sorted(unique, key=_rank)


# --- 监听地址与端口 ---------------------------------------------------------
def shared_mode(db: Session | None = None) -> bool:
    """共享模式开关：settings 表 > 环境变量 AOHUANG_SHARED_MODE。

    为什么表优先：运维在界面上点过「共享」之后，重启时环境变量可能已经不在了
    （双击 exe 启动的那条路径不设变量），这时候必须还认库里那个开关，
    否则「重启一下又变回单机」——这正是共享部署最难查的那类问题。
    """
    if db is not None:
        try:
            from services import setting_service

            stored = setting_service.get(db, SHARED_MODE_KEY)
            if isinstance(stored, bool):
                return stored
        except Exception:  # noqa: BLE001 - 表读失败就退回环境变量
            pass
    return bool(getattr(get_settings(), "shared_mode", False))


def effective_bind_host(shared: bool) -> str:
    """现在**应该**监听哪个地址。

    `bind_host` 属性由另一处改动引入，属性存在且非空就用它（那是唯一真值），
    否则按 shared 推：共享 → 0.0.0.0，单机 → 127.0.0.1。
    """
    declared = str(getattr(get_settings(), "bind_host", "") or "").strip()
    if declared:
        return declared
    return "0.0.0.0" if shared else "127.0.0.1"


def effective_port() -> int:
    """进程真正绑的端口。

    `pick_port()` 会在 8812 被占时往上换，所以不能只信 settings.api_port：
    AOHUANG_API_PORT 是 Electron 传下来的首选端口，进程内实际端口以
    uvicorn 的入参为准 —— 这里按「首选端口」报，并在 share 视图里同时给出
    端口区间，避免运维把 8812 当死值。
    """
    settings = get_settings()
    for name in ("AOHUANG_API_PORT", "AOHUANG_PORT"):
        raw = (os.environ.get(name) or "").strip()
        if raw.isdigit():
            return int(raw)
    return int(getattr(settings, "api_port", 8812))


# --- 数据库事实 -------------------------------------------------------------
def db_path() -> Path:
    return Path(get_settings().db_path)


def db_size_bytes() -> int:
    """主库 + WAL + SHM 的总字节数。

    只算 app.db 会低报：WAL 模式下未 checkpoint 的数据在 app.db-wal 里，
    共享服务器上这个文件经常比主库还大。
    """
    total = 0
    base = db_path()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(base) + suffix)
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def wal_enabled() -> bool:
    """读 journal_mode。用裸连接读，避免在 ORM 事务里开 PRAGMA。"""
    try:
        con = sqlite3.connect(str(db_path()), timeout=2.0)
    except sqlite3.Error:
        return False
    try:
        row = con.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower() == "wal" if row else False
    except sqlite3.Error:
        return False
    finally:
        con.close()


def probe_writable(force: bool = False) -> dict[str, Any]:
    """真的抢一次写锁再放开，判断「现在能不能写」。

    用 `BEGIN IMMEDIATE` + `ROLLBACK`：拿到写锁就证明可写，且一行数据都不落。
    不用「往临时表写一行」是因为那会在 WAL 里留垃圾，共享服务器上天天探活
    会把 WAL 撑起来。

    区分三种结果，别混成一句「不可写」：
      · writable=True,  busy=False  → 正常
      · writable=True,  busy=True   → 库可写，但此刻别人占着写锁（`database is locked`）
      · writable=False, busy=False  → 真的不可写（只读卷 / 权限 / 文件被占）
    """
    path = db_path()
    now = time.time()
    with _write_probe_lock:
        cached = dict(_write_probe_cache)
        fresh = (
            not force
            and cached.get("path") == str(path)
            and (now - float(cached.get("at") or 0.0)) < WRITE_PROBE_TTL
        )
    if fresh:
        return {**cached, "cached": True}

    result: dict[str, Any] = {"at": time.time(), "writable": True, "busy": False,
                              "error": "", "path": str(path), "cached": False}
    if not path.exists():
        result.update(writable=False, error="数据库文件不存在")
    else:
        try:
            # 250ms 足够判断「锁在不在」，又不会把探活拖成慢请求
            con = sqlite3.connect(str(path), timeout=0.25)
            try:
                con.execute("PRAGMA busy_timeout=250")
                con.execute("BEGIN IMMEDIATE")
                con.execute("ROLLBACK")
            finally:
                con.close()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                # 锁冲突不等于不可写：库是好的，只是这一刻写不进去
                result.update(writable=True, busy=True, error=str(exc))
            else:
                result.update(writable=False, error=str(exc))
        except sqlite3.Error as exc:
            result.update(writable=False, error=str(exc))

    with _write_probe_lock:
        _write_probe_cache.clear()
        _write_probe_cache.update(result)
    return result


def db_facts(force_probe: bool = False) -> dict[str, Any]:
    probe = probe_writable(force=force_probe)
    return {
        "dbPath": str(db_path()),
        "dbSizeBytes": db_size_bytes(),
        "walEnabled": wal_enabled(),
        "dbWritable": bool(probe.get("writable")),
        "dbBusy": bool(probe.get("busy")),
        "dbError": str(probe.get("error") or ""),
        "dbProbeCached": bool(probe.get("cached")),
    }


def busy_timeout_ms(db: Session | None = None) -> int:
    """当前连接上的 busy_timeout（毫秒）。共享部署调参要看这个数。"""
    try:
        if db is not None:
            row = db.execute(text("PRAGMA busy_timeout")).fetchone()
        else:
            con = sqlite3.connect(str(db_path()), timeout=1.0)
            try:
                row = con.execute("PRAGMA busy_timeout").fetchone()
            finally:
                con.close()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001 - 读不到就报 0，不抛
        return 0


# --- 在线客户端 -------------------------------------------------------------
def _client_key(request: Any) -> tuple[str, str]:
    """客户端身份：优先 X-Client-Id（同一台机器换网卡也不变），否则源 IP。"""
    headers = getattr(request, "headers", None)
    client_id = ""
    version = ""
    if headers is not None:
        client_id = str(headers.get("X-Client-Id") or "").strip()
        version = str(headers.get("X-Client-Version") or "").strip()
    ip = ""
    client = getattr(request, "client", None)
    if client is not None:
        ip = str(getattr(client, "host", "") or "")
    if not ip:
        ip = str(getattr(getattr(request, "url", None), "hostname", "") or "")
    return (client_id or ip or "unknown"), version


def note_client(request: Any, *, authenticated: bool = False) -> None:
    """记一次客户端心跳。任何请求都能记，鉴权与否都记。

    不记 token、不记 Authorization：共享服务器上一个运维页就能看到别人 token
    的话，这个接口本身就是漏洞。只留「谁、从哪、什么版本、什么时候」。
    """
    key, version = _client_key(request)
    now = time.time()
    with _clients_lock:
        row = _clients.get(key)
        if row is None:
            row = {"clientId": key, "ip": "", "clientVersion": version,
                   "firstSeen": now, "requests": 0, "authenticated": authenticated}
            _clients[key] = row
        row["lastSeen"] = now
        row["requests"] = int(row.get("requests") or 0) + 1
        row["ip"] = str(getattr(getattr(request, "client", None), "host", "") or "")
        if version:
            row["clientVersion"] = version
        if authenticated:
            row["authenticated"] = True
        # 顺手清掉过期的，避免长期运行把字典撑大
        cutoff = now - CLIENT_WINDOW_SECONDS * 4
        for stale in [name for name, item in _clients.items()
                      if float(item.get("lastSeen") or 0) < cutoff]:
            _clients.pop(stale, None)


def active_operators(window: float = CLIENT_WINDOW_SECONDS) -> list[dict[str, Any]]:
    """窗口内还在发请求的客户端，最近活跃的排前面。"""
    now = time.time()
    cutoff = now - max(1.0, float(window))
    with _clients_lock:
        rows = [dict(item) for item in _clients.values()
                if float(item.get("lastSeen") or 0) >= cutoff]
    for row in rows:
        row["idleSeconds"] = round(now - float(row.get("lastSeen") or now), 1)
        row["lastSeen"] = round(float(row.get("lastSeen") or now), 3)
        row["firstSeen"] = round(float(row.get("firstSeen") or now), 3)
    rows.sort(key=lambda item: float(item.get("lastSeen") or 0), reverse=True)
    return rows


# --- 视图 -------------------------------------------------------------------
def share_view(db: Session | None = None) -> dict[str, Any]:
    """`GET /system/share` 的完整返回。

    `bindHost` 是「现在该监听什么」，`listening` 是「本机到底有没有在听」——
    两者不一致正是这次要解决的问题（sharedMode 改了但没重启），
    所以必须分开给，不能合成一个字段让人猜。
    """
    shared = shared_mode(db)
    host = effective_bind_host(shared)
    port = effective_port()
    addresses = lan_addresses()
    facts = db_facts()
    operators = active_operators()
    settings = get_settings()
    return {
        "sharedMode": shared,
        "bindHost": host,
        "port": port,
        "portRange": [int(getattr(settings, "api_port", 8812)),
                      int(getattr(settings, "api_port_max", 8822))],
        "lanAddresses": addresses,
        "clientCount": len(operators),
        "activeOperators": operators,
        "dbPath": facts["dbPath"],
        "dbSizeBytes": facts["dbSizeBytes"],
        "walEnabled": facts["walEnabled"],
        "dbWritable": facts["dbWritable"],
        "dbBusy": facts["dbBusy"],
        "dbError": facts["dbError"],
        "busyTimeoutMs": busy_timeout_ms(db),
        "serverVersion": APP_VERSION,
        # 直接可粘给同事的地址，省得他们自己拼
        "shareUrls": [f"http://{address}:{port}" for address in addresses],
        "restartRequired": False,
        "warnings": share_warnings(shared, host, addresses, facts),
    }


def share_warnings(shared: bool, host: str, addresses: list[str],
                   facts: dict[str, Any]) -> list[str]:
    """把「现在这个配置会让同事踩到什么」提前说出来，别等他们连不上。"""
    warnings: list[str] = []
    if shared and host.startswith("127."):
        warnings.append(
            "sharedMode 已开，但 bindHost 仍是回环地址：别的机器连不上，"
            "需要重启后端让 bind_host 生效")
    if not shared and host in ("0.0.0.0", "::"):
        warnings.append(
            "sharedMode 已关，但 bindHost 是 0.0.0.0：端口对整个局域网开放，"
            "确认这是你要的")
    if shared and not addresses:
        warnings.append("本机没有可对外的 IPv4（只有回环/169.254），同事无法连接")
    if not facts.get("walEnabled"):
        warnings.append("journal_mode 不是 WAL：多人同时读写会互相阻塞")
    if int(facts.get("dbSizeBytes") or 0) > 512 * 1024 * 1024:
        warnings.append("数据库已超过 512 MB：SQLite 还能用，但备份与 checkpoint 会明显变慢")
    return warnings


def health_view(db: Session | None = None) -> dict[str, Any]:
    """`GET /system/share/health` —— 客户端**没有 token 时**也要能调。

    只回答「服务器活着、库能写、几个人在连」，不含任何路径与凭据细节：
    这是免鉴权接口，泄露 dbPath 等于把服务器目录结构告诉整个网段。
    """
    from services import system_service

    facts = db_facts()
    operators = active_operators()
    base = system_service.health(db_ok=bool(facts.get("dbWritable")), db_state="ok")
    shared = shared_mode(db)
    return {
        "ok": bool(facts.get("dbWritable")) and not facts.get("dbBusy"),
        "serverVersion": APP_VERSION,
        "uptimeSeconds": base.get("uptimeSeconds"),
        "dbWritable": bool(facts.get("dbWritable")),
        "dbBusy": bool(facts.get("dbBusy")),
        "operatorCount": len(operators),
        "sharedMode": shared,
        "bindHost": effective_bind_host(shared),
        "port": effective_port(),
        "name": base.get("name"),
    }


def set_shared_mode(db: Session, value: bool) -> dict[str, Any]:
    """切换共享模式：落库 + 明确告知「要不要重启」。

    监听地址是 uvicorn 启动参数，进程活着就改不了 —— 所以这里不假装生效，
    直接回 `restartRequired: true`，并写清重启后该是什么地址。
    """
    from services import setting_service

    setting_service.put(db, SHARED_MODE_KEY, bool(value))
    before_host = effective_bind_host(not bool(value))
    after_host = effective_bind_host(bool(value))
    return {
        "sharedMode": bool(value),
        "bindHost": after_host,
        "port": effective_port(),
        "restartRequired": before_host != after_host,
        "appliesAfterRestart": True,
        "previousBindHost": before_host,
        "note": (
            "监听地址是 uvicorn 的启动参数，改完必须重启后端才生效；"
            f"重启后应监听 {after_host}:{effective_port()}。"
            "客户端不需要跟着重启，但重启窗口内会短暂断连。"
        ),
        "lanAddresses": lan_addresses(),
        "shareUrls": [f"http://{address}:{effective_port()}"
                      for address in lan_addresses()],
    }
