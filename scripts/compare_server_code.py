"""服务器与本地 Git 工作区的后端代码一致性校验。

用法（本地跑，通过 SSH 远程比对）：
    python scripts/compare_server_code.py

只比对后端会实际运行的目录（api/services/models/core/schemas/migrations），
前端在客户端安装包里，不在服务器上。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

SERVER_DIR = "/opt/miaoshou-listing-v2/server"
LOCAL_ROOT = Path(__file__).resolve().parent.parent
DIRS = ("api", "services", "models", "core", "schemas", "migrations", "scripts")
SUFFIXES = (".py",)


def local_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for name in DIRS:
        base = LOCAL_ROOT / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(LOCAL_ROOT).as_posix()
            data = path.read_bytes().replace(b"\r\n", b"\n")
            result[rel] = hashlib.sha256(data).hexdigest()
    return result


def remote_hashes() -> dict[str, str]:
    script = (
        "import hashlib, pathlib\n"
        "root = pathlib.Path(%r)\n"
        "dirs = %r\n"
        "out = {}\n"
        "for name in dirs:\n"
        "    base = root / name\n"
        "    if not base.is_dir():\n"
        "        continue\n"
        "    for p in base.rglob('*.py'):\n"
        "        if '__pycache__' in p.parts:\n"
        "            continue\n"
        "        rel = p.relative_to(root).as_posix()\n"
        "        out[rel] = hashlib.sha256(p.read_bytes().replace(b'\\r\\n', b'\\n')).hexdigest()\n"
        "for k, v in sorted(out.items()):\n"
        "    print(v + '  ' + k)\n"
    ) % (SERVER_DIR, DIRS)
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "root@38.95.75.47",
         "/opt/miaoshou-listing-v2/venv/bin/python -c " + repr(script)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        print("远程读取失败:", proc.stderr[:400])
        return {}
    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("  ", 1)
        if len(parts) == 2:
            result[parts[1]] = parts[0]
    return result


def main() -> int:
    local = local_hashes()
    remote = remote_hashes()
    if not remote:
        return 1
    only_local = sorted(set(local) - set(remote))
    only_remote = sorted(set(remote) - set(local))
    differ = sorted(k for k in set(local) & set(remote) if local[k] != remote[k])

    print("本地文件: %d | 服务器文件: %d" % (len(local), len(remote)))
    print("一致: %d" % (len(set(local) & set(remote)) - len(differ)))
    if differ:
        print("\n内容不同 (%d):" % len(differ))
        for k in differ:
            print("  ", k)
    if only_local:
        print("\n仅本地有 (%d):" % len(only_local))
        for k in only_local:
            print("  ", k)
    if only_remote:
        print("\n仅服务器有 (%d):" % len(only_remote))
        for k in only_remote[:40]:
            print("  ", k)
        if len(only_remote) > 40:
            print("   ... 还有 %d 个" % (len(only_remote) - 40))
    ok = not differ and not only_local
    print("\n%s" % ("✅ 服务器与本地一致" if ok else "⚠️  存在差异"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
