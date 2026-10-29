"""Alembic 迁移链自检（全部在临时目录里跑，不碰真实 app.db）。

依次验证四件事：
  1. 空库 upgrade head → 建出全部表
  2. 在已迁移的库上再跑一次 autogenerate → 应该没有 diff（模型与迁移无漂移）
  3. downgrade base → 业务表清空
  4. 老库场景：create_all 建好的库（有表、无 alembic_version）
     → `alembic stamp head` → 后续 upgrade 正常工作

用法（在 server/ 下）：
    python migrations/_selftest.py

每步都打印实际观测值，退出码 0 表示全部通过。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent


def _utf8_stdio() -> None:
    """Windows 控制台默认 GBK，中文报告会变乱码；CI 是 UTF-8，不受影响。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")


def _business_tables(db_path: Path) -> list[str]:
    """业务表列表（排除 sqlite 内部表与 alembic_version）。"""
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        return [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name")]


def _run(args: list[str], data_dir: Path) -> subprocess.CompletedProcess:
    """跑一条子命令（args 直接拼在 `python` 后面，可以是 `-m alembic ...` 或 `-c ...`）。"""
    env = dict(os.environ)
    env["AOHUANG_DATA_DIR"] = str(data_dir)
    env["AOHUANG_DATABASE_URL"] = ""
    env["PYTHONPATH"] = str(SERVER_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, *args], cwd=str(SERVER_DIR), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _alembic(*args: str, data_dir: Path) -> subprocess.CompletedProcess:
    return _run(["-m", "alembic", *args], data_dir)


def _autogenerate_diff_count(data_dir: Path) -> int:
    """跑一次 autogenerate，返回生成文件里 upgrade() 的非注释操作条数。

    没有漂移时 alembic 会生成一个空迁移（upgrade 里只有 pass）。
    探针文件必须在读完立刻删掉：它会挂在迁移链尾部（down_revision 指向
    head），留在仓库里会让下一次真实的 autogenerate 接在探针后面。
    """
    versions_dir = SERVER_DIR / "migrations" / "versions"
    before = set(versions_dir.glob("*.py"))
    proc = _alembic("revision", "--autogenerate", "-m", "_selftest drift probe",
                    data_dir=data_dir)
    new_files = sorted(set(versions_dir.glob("*.py")) - before)
    try:
        if proc.returncode != 0:
            print(proc.stdout, proc.stderr, sep="\n", file=sys.stderr)
            raise SystemExit("autogenerate 失败")
        if not new_files:
            raise SystemExit("autogenerate 没有生成文件，无法判断漂移")
        text = new_files[0].read_text(encoding="utf-8")
        # 统计 upgrade() 里真正的操作行（排除注释、空行、pass）
        body = text.split("def upgrade() -> None:")[1].split("def downgrade()")[0]
        return len([line for line in body.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                    and line.strip() != "pass"])
    finally:
        for path in new_files:
            path.unlink(missing_ok=True)


def _expected_tables() -> set[str]:
    """模型里应有的表名集合（从 Base.metadata 派生，不硬编码数量）。

    硬编码 13 的后果：别人加一个模型，自检立刻报「数量不对」，
    但真正该被发现的是「迁移漏建了那张表」—— 集合比对才能指出是哪张。
    子进程隔离：db.session 的 engine 绑在导入时的 data_dir 上，
    这个函数只读 metadata，不碰库，所以同进程调用是安全的。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SERVER_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="aohuang-selftest-meta-")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import models; from db.session import Base; "
         "print('\\n'.join(sorted(Base.metadata.tables)))"],
        cwd=str(SERVER_DIR), env=env, capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        print(proc.stdout, proc.stderr, sep="\n", file=sys.stderr)
        raise SystemExit("读取 Base.metadata 失败")
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def main() -> int:
    _utf8_stdio()
    failures: list[str] = []
    expected = _expected_tables()
    print(f"[0] 模型里的表：{len(expected)} 张")

    # ---------- 场景 1：空库 upgrade head ----------
    fresh = Path(tempfile.mkdtemp(prefix="aohuang-selftest-fresh-"))
    proc = _alembic("upgrade", "head", data_dir=fresh)
    tables = set(_business_tables(fresh / "app.db"))
    print(f"[1] 空库 upgrade head：exit={proc.returncode} 业务表={len(tables)}")
    print("    " + " / ".join(sorted(tables)))
    if proc.returncode != 0:
        failures.append("空库 upgrade head 失败")
        print(proc.stdout, proc.stderr, sep="\n", file=sys.stderr)
    missing = expected - tables
    extra = tables - expected
    if missing:
        failures.append(f"迁移漏建：{sorted(missing)}")
    if extra:
        failures.append(f"迁移多建（模型里没有）：{sorted(extra)}")

    # ---------- 场景 2：再跑一次 autogenerate 应无漂移 ----------
    drift = _autogenerate_diff_count(fresh)
    print(f"[2] 迁移后再 autogenerate：upgrade() 操作条数={drift}（应为 0）")
    if drift != 0:
        failures.append(f"模型与迁移存在漂移：autogenerate 产出 {drift} 条操作")

    # ---------- 场景 3：downgrade base ----------
    proc = _alembic("downgrade", "base", data_dir=fresh)
    remaining = _business_tables(fresh / "app.db")
    print(f"[3] downgrade base：exit={proc.returncode} 剩余业务表={len(remaining)}（应为 0）")
    if proc.returncode != 0:
        failures.append("downgrade base 失败")
        print(proc.stdout, proc.stderr, sep="\n", file=sys.stderr)
    if remaining:
        failures.append(f"downgrade base 未清空：{remaining}")

    # ---------- 场景 4：老库（create_all 建好、无版本表）走 stamp ----------
    legacy = Path(tempfile.mkdtemp(prefix="aohuang-selftest-legacy-"))
    proc = _run(["-c", "from db.session import init_db; init_db()"], legacy)
    legacy_tables = _business_tables(legacy / "app.db")
    print(f"[4a] 老库 create_all：exit={proc.returncode} 业务表={len(legacy_tables)}")
    if proc.returncode != 0:
        failures.append("老库 create_all 失败")
        print(proc.stdout, proc.stderr, sep="\n", file=sys.stderr)

    # 老库直接 upgrade 会因为表已存在而失败 —— 这正是必须走 stamp 的原因
    direct = _alembic("upgrade", "head", data_dir=legacy)
    print(f"[4b] 老库直接 upgrade head：exit={direct.returncode}"
          f"（预期非 0：表已存在）")
    if direct.returncode == 0:
        failures.append("老库直接 upgrade 竟然成功了，说明基线没建表？")

    stamp = _alembic("stamp", "head", data_dir=legacy)
    after_stamp = _alembic("upgrade", "head", data_dir=legacy)
    with sqlite3.connect(legacy / "app.db") as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    print(f"[4c] 老库 stamp head：exit={stamp.returncode}，"
          f"之后 upgrade head：exit={after_stamp.returncode}，版本表={version}")
    if stamp.returncode != 0 or after_stamp.returncode != 0:
        failures.append("老库 stamp 流程失败")
        print(stamp.stdout, stamp.stderr, sep="\n", file=sys.stderr)
    if not version:
        failures.append("stamp 后 alembic_version 为空")
    stamped_tables = set(_business_tables(legacy / "app.db"))
    if stamped_tables != expected:
        failures.append(
            f"stamp 后老库表集合与模型不一致："
            f"缺 {sorted(expected - stamped_tables)} / 多 {sorted(stamped_tables - expected)}")

    print()
    if failures:
        print("[结论] 自检未通过：", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"[结论] 迁移链自检全部通过（{len(expected)} 表 / 无漂移 / downgrade 清空 / 老库 stamp）")
    # 清理临时库
    for path in (fresh, legacy):
        shutil.rmtree(path, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
