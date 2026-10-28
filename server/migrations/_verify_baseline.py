"""比对「模型 create_all 出来的表」与「基线迁移 upgrade 出来的表」是否等价。

用法（在 server/ 下）：
    python migrations/_verify_baseline.py

两份库各建在自己的临时目录、各自的子进程里（`db.session.engine` 是模块级
单例，同一进程里换 AOHUANG_DATA_DIR 不会生效，所以必须隔离进程）。
全程不碰真实 app.db。输出逐表逐列差异，退出码 0 表示完全一致。

这个脚本是「基线迁移没漏东西」的证据生成器，不参与应用运行，也不需要进 CI
（CI 里已经有 upgrade head / downgrade base 的冒烟）。留着是为了下次改
models 之后能一条命令复核。
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))


def table_signature(db_path: Path) -> dict[str, dict]:
    """把库结构读成可比较的字典：列类型/非空/默认值、索引、外键。"""
    out: dict[str, dict] = {}
    with sqlite3.connect(db_path) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name")]
        for table in tables:
            columns = {}
            for row in conn.execute(f'PRAGMA table_info("{table}")'):
                # cid, name, type, notnull, dflt_value, pk
                columns[row[1]] = [row[2].upper(), bool(row[3]), row[4], bool(row[5])]
            indexes = set()
            for row in conn.execute(f'PRAGMA index_list("{table}")'):
                # seq, name, unique, origin, partial
                # origin=u 是 UNIQUE 约束自动建的索引，一并收进来（约束也是结构）
                indexes.add(f"{row[1]}|unique={bool(row[2])}|origin={row[3]}")
            fks = set()
            for row in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
                # id, seq, table, from, to, on_update, on_delete, match
                fks.add(f"{row[3]}->{row[2]}.{row[4]}|ondelete={row[6]}")
            out[table] = {
                "columns": columns,
                "indexes": sorted(indexes),
                "fks": sorted(fks),
            }
    return out


def _run_child(mode: str, workdir: Path) -> dict[str, dict]:
    """在独立子进程里建库，返回结构签名。

    隔离进程的原因：`db/session.py` 在 import 时就把 engine 绑到了当时的
    `AOHUANG_DATA_DIR`，同进程内改环境变量无效，两个库会写进同一个文件。
    """
    env = dict(os.environ)
    env["AOHUANG_DATA_DIR"] = str(workdir)
    env["PYTHONPATH"] = str(SERVER_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", mode],
        cwd=str(SERVER_DIR), env=env, capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        print(proc.stdout, proc.stderr, sep="\n", file=sys.stderr)
        raise SystemExit(f"子进程（{mode}）失败")
    return json.loads(proc.stdout)


def _child(mode: str) -> int:
    """子进程入口：建库并把签名打到 stdout。"""
    from core.config import get_settings

    settings = get_settings()
    if mode == "create-all":
        from db.session import init_db

        init_db()
    elif mode == "alembic":
        from alembic import command
        from alembic.config import Config

        ini = Config(str(SERVER_DIR / "alembic.ini"))
        ini.set_main_option("script_location", str(SERVER_DIR / "migrations"))
        command.upgrade(ini, "head")
    else:
        raise SystemExit(f"未知模式：{mode}")

    print(json.dumps(table_signature(settings.db_path), ensure_ascii=False))
    return 0


def main() -> int:
    workdirs = {
        mode: Path(tempfile.mkdtemp(prefix=f"aohuang-verify-{mode}-"))
        for mode in ("create-all", "alembic")
    }
    left = _run_child("create-all", workdirs["create-all"])
    right = _run_child("alembic", workdirs["alembic"])

    ok = True
    if set(left) != set(right):
        ok = False
        print(f"[表集合] create_all={len(left)} 迁移={len(right)}")
        print(f"  只在 create_all：{sorted(set(left) - set(right))}")
        print(f"  只在迁移：      {sorted(set(right) - set(left))}")

    for table in sorted(set(left) & set(right)):
        a, b = left[table], right[table]
        if set(a["columns"]) != set(b["columns"]):
            ok = False
            print(f"[{table}] 列集合不同")
            print(f"  只在 create_all：{sorted(set(a['columns']) - set(b['columns']))}")
            print(f"  只在迁移：      {sorted(set(b['columns']) - set(a['columns']))}")
        for col in sorted(set(a["columns"]) & set(b["columns"])):
            if a["columns"][col] != b["columns"][col]:
                ok = False
                print(f"[{table}.{col}] create_all={a['columns'][col]} 迁移={b['columns'][col]}")
        if a["indexes"] != b["indexes"]:
            ok = False
            print(f"[{table}] 索引不同")
            print(f"  只在 create_all：{sorted(set(a['indexes']) - set(b['indexes']))}")
            print(f"  只在迁移：      {sorted(set(b['indexes']) - set(a['indexes']))}")
        if a["fks"] != b["fks"]:
            ok = False
            print(f"[{table}] 外键不同")
            print(f"  只在 create_all：{sorted(set(a['fks']) - set(b['fks']))}")
            print(f"  只在迁移：      {sorted(set(b['fks']) - set(a['fks']))}")

    print()
    print(f"表数量：create_all={len(left)} / 迁移={len(right)}")
    print(f"索引数量：create_all={sum(len(v['indexes']) for v in left.values())} / "
          f"迁移={sum(len(v['indexes']) for v in right.values())}")
    print(f"外键数量：create_all={sum(len(v['fks']) for v in left.values())} / "
          f"迁移={sum(len(v['fks']) for v in right.values())}")
    if ok:
        print("[结论] 基线迁移与 create_all 完全等价"
              "（列类型 / 非空 / 默认值 / 主键 / 索引 / 外键）")
    else:
        print("[结论] 存在差异，见上方逐条输出", file=sys.stderr)
    return 0 if ok else 1


def _utf8_stdio() -> None:
    """Windows 控制台默认 GBK，中文报告会变乱码；CI 是 UTF-8，不受影响。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")


if __name__ == "__main__":
    _utf8_stdio()
    if "--child" in sys.argv:
        raise SystemExit(_child(sys.argv[sys.argv.index("--child") + 1]))
    raise SystemExit(main())
