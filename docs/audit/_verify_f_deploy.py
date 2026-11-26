"""审计 F 复核的可复现验证脚本（只读，不修改服务器任何内容）。

用法：
    python -X utf8 docs/audit/_verify_f_deploy.py

三项检查：
  1. 生产运行目录（api/services/models/core/schemas/db + main.py）逐文件 sha256
  2. 生产 server/ 下本地已不存在的残留文件
  3. 生产目录权限 + cron 脚本可写性（提权面）

## 换行符陷阱（2026-11-26 复核教训）

比对本地与服务器代码时**必须先统一换行**，否则 CRLF/LF 差异会伪装成代码漂移。
实测案例：`services/source_link.py` 本地 588 个 CRLF、远端纯 LF，
raw sha256 不同（26179B vs 25591B，差 588 字节 = 588 行 × 1 字节），
但文本 diff 为 0 行 —— 内容完全一致。

所以本脚本所有哈希都在 `.replace(b"\\r\\n", b"\\n")` 之后计算，
与 `scripts/compare_server_code.py:34` 的口径一致。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import paramiko

HOST = "38.95.75.47"
USER = "root"
PASSWORD_FILE = Path(r"C:\Users\Administrator\Desktop\雨云.txt")
REMOTE_BASE = "/opt/miaoshou-listing-v2/server"
LOCAL_BASE = Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\server")
PY = "/opt/miaoshou-listing-v2/venv/bin/python"

#: 服务实际 import 的包（systemd WorkingDirectory + uvicorn main:app）
PROD_DIRS = ("api", "services", "models", "core", "schemas", "db")
PROD_ROOT_FILES = ("main.py", "run_server.py")

REMOTE_TREE_SCRIPT = '''# -*- coding: utf-8 -*-
import hashlib, json, pathlib
root = pathlib.Path("%s")
dirs = ("api", "services", "models", "core", "schemas", "db")
out = {}
for d in dirs:
    base = root / d
    if not base.is_dir():
        continue
    for p in base.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        data = p.read_bytes().replace(b"\\r\\n", b"\\n")
        out[p.relative_to(root).as_posix()] = hashlib.sha256(data).hexdigest()
for name in ("main.py", "run_server.py"):
    p = root / name
    if p.is_file():
        out[name] = hashlib.sha256(
            p.read_bytes().replace(b"\\r\\n", b"\\n")).hexdigest()
print("@@JSON@@" + json.dumps(out))
''' % REMOTE_BASE

PERM_CMDS = [
    ("生产运行目录权限",
     f"stat -c '%A %U:%G %n' {REMOTE_BASE} " +
     " ".join(f"{REMOTE_BASE}/{d}" for d in PROD_DIRS) +
     f" {REMOTE_BASE}/scripts"),
    ("root cron 执行什么",
     "cat /etc/cron.d/aohuang-img-migrate"),
    ("cron 脚本权限",
     f"stat -c '%A %U:%G %n' {REMOTE_BASE}/scripts/migrate_autotemp.py "
     f"{REMOTE_BASE}/scripts/image_zones.py"),
    ("低权限账户能否写 cron 脚本（提权面）",
     f"runuser -u www-data -- test -w {REMOTE_BASE}/scripts/image_zones.py "
     "&& echo 'YES www-data 可写' || echo 'NO'"),
    ("服务运行用户",
     "systemctl show aohuang-listing-v2 -p User -p Group"),
    ("生产 server/ 根目录残留",
     f"ls -a {REMOTE_BASE}/"),
]


def read_pw() -> str:
    lines = [x.strip() for x in PASSWORD_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
    return lines[-1]


def connect() -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, USER, read_pw(), timeout=30, allow_agent=False, look_for_keys=False)
    return c


def local_hashes() -> dict[str, str]:
    out: dict[str, str] = {}
    for d in PROD_DIRS:
        base = LOCAL_BASE / d
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            data = p.read_bytes().replace(b"\r\n", b"\n")
            out[p.relative_to(LOCAL_BASE).as_posix()] = hashlib.sha256(data).hexdigest()
    for name in PROD_ROOT_FILES:
        p = LOCAL_BASE / name
        if p.is_file():
            out[name] = hashlib.sha256(
                p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return out


def check1(c: paramiko.SSHClient) -> None:
    print("=" * 74)
    print("① 生产运行目录逐文件 sha256（换行已统一为 LF）")
    print("=" * 74)
    local = local_hashes()
    sftp = c.open_sftp()
    with sftp.open("/tmp/_audit_prod_tree.py", "w") as fh:
        fh.write(REMOTE_TREE_SCRIPT)
    sftp.close()
    _, out, err = c.exec_command(f"{PY} /tmp/_audit_prod_tree.py", timeout=180)
    raw = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    if "@@JSON@@" not in raw:
        print("  远程执行失败:", e[:400])
        return
    remote = json.loads(raw.split("@@JSON@@", 1)[1])
    differ = sorted(k for k in set(local) & set(remote) if local[k] != remote[k])
    only_local = sorted(set(local) - set(remote))
    only_remote = sorted(set(remote) - set(local))
    print(f"  本地 {len(local)} / 远端 {len(remote)}")
    print(f"  内容不同 {len(differ)} / 仅本地 {len(only_local)} / 仅远端 {len(only_remote)}")
    for k in differ:
        print(f"    DIFF {k}")
    for k in only_local:
        print(f"    ONLY-LOCAL {k}")
    for k in only_remote:
        print(f"    ONLY-REMOTE {k}")
    if not (differ or only_local or only_remote):
        print("  ✅ 生产运行目录与本地仓库完全一致")


def check2(c: paramiko.SSHClient) -> None:
    print()
    print("=" * 74)
    print("② 生产 server/ 下本地已不存在的残留文件")
    print("=" * 74)
    local = set()
    for p in LOCAL_BASE.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            local.add(p.relative_to(LOCAL_BASE).as_posix())
    _, out, _ = c.exec_command(
        f"cd {REMOTE_BASE} && find . -type f -name '*.py' -not -path '*__pycache__*' "
        f"| sed 's|^\\./||'", timeout=120)
    remote = {line.strip() for line in out.read().decode("utf-8", "replace").splitlines()
              if line.strip()}
    stale = sorted(remote - local)
    print(f"  本地 {len(local)} / 远端 {len(remote)} / 仅远端（残留）{len(stale)}")
    top_level = [s for s in stale if "/" not in s]
    in_scripts = [s for s in stale if s.startswith("scripts/")]
    other = [s for s in stale if s not in top_level and s not in in_scripts]
    print(f"\n  根目录残留（{len(top_level)}）:")
    for s in top_level:
        print("    ", s)
    print(f"\n  scripts/ 残留（{len(in_scripts)}）:")
    for s in in_scripts[:40]:
        print("    ", s)
    if len(in_scripts) > 40:
        print(f"     ... 还有 {len(in_scripts) - 40} 个")
    print(f"\n  其他残留（{len(other)}）:")
    for s in other:
        print("    ", s)


def check3(c: paramiko.SSHClient) -> None:
    print()
    print("=" * 74)
    print("③ 生产目录权限 + cron 脚本可写性（提权面）")
    print("=" * 74)
    for label, cmd in PERM_CMDS:
        _, o, e = c.exec_command(cmd, timeout=90)
        print(f"\n  ### {label}")
        print("  $ " + cmd)
        text = o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")
        for line in text.splitlines():
            print("  " + line)


def main() -> int:
    c = connect()
    try:
        check1(c)
        check2(c)
        check3(c)
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
