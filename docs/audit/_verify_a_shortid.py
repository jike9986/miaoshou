"""验证审计 A 的 P0-1：short_id 在三条建库路径下是否有唯一约束。

三条路径：
  1. alembic upgrade head（新库正路）
  2. create_all（init_db 走的路径）
  3. 老库升级（缺列时 _add_missing_columns 补列）

审计 A 声称：alembic 路径缺列 → PostgreSQL 崩；老库升级只能补列、补不上唯一约束。
这个脚本用实测数据裁决。
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "server"
# 子进程必须把 server/ 放进 sys.path（等价于 `cd server` 后运行）
CHILD_PREAMBLE = "import sys; sys.path.insert(0, %r)\n" % str(SERVER)


def inspect_db(dbpath: str, label: str) -> None:
    if not Path(dbpath).exists():
        print(f"{label}: 数据库文件不存在 {dbpath}")
        return
    con = sqlite3.connect(dbpath)
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operators'"
    ).fetchone()
    if not row:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        print(f"{label}: operators 表不存在。现有表 {len(tables)} 个")
        con.close()
        return
    ddl = row[0]
    has_col = any("short_id" in line for line in ddl.splitlines())
    has_table_uq = "UNIQUE (short_id)" in ddl or "UNIQUE(short_id)" in ddl
    idx = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='operators'"
    ).fetchall()
    uq_idx = [n for n, s in idx if s and "short_id" in s]
    print(f"{label}:")
    print(f"    short_id 列存在     : {has_col}")
    print(f"    建表级 UNIQUE        : {has_table_uq}")
    print(f"    short_id 唯一索引    : {uq_idx or '无'}")
    print(f"    → 唯一约束是否成立  : {has_table_uq or bool(uq_idx)}")
    con.close()


def path1_alembic() -> None:
    tmp = tempfile.mkdtemp(prefix="audit-alembic-")
    env = {**os.environ, "AOHUANG_DATA_DIR": tmp, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True, env=env, cwd=str(SERVER),
    )
    print(f"路径1 alembic upgrade head → exit {result.returncode}")
    if result.returncode != 0:
        print(result.stderr.decode("utf-8", "replace")[-800:])
    # alembic env.py 里数据库位置由 AOHUANG_DATA_DIR 决定
    for candidate in Path(tmp).rglob("*.db"):
        inspect_db(str(candidate), f"路径1 alembic（{candidate.name}）")


def path2_create_all() -> None:
    tmp = tempfile.mkdtemp(prefix="audit-createall-")
    code = CHILD_PREAMBLE + """
import os, sys, tempfile
os.environ["AOHUANG_DATA_DIR"] = sys.argv[1]
from db.session import init_db, engine
init_db()
print("OK", engine.url)
"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, "-c", code, tmp],
        capture_output=True, env=env, cwd=str(SERVER),
    )
    print(f"\n路径2 init_db/create_all → exit {result.returncode}")
    print(result.stdout.decode("utf-8", "replace").strip()[:200])
    if result.returncode != 0:
        print(result.stderr.decode("utf-8", "replace")[-800:])
    for candidate in Path(tmp).rglob("*.db"):
        inspect_db(str(candidate), f"路径2 create_all（{candidate.name}）")


def path3_legacy_upgrade() -> None:
    """造一个老库（operators 表没有 short_id），再跑 init_db 看能否补上唯一约束。"""
    tmp = tempfile.mkdtemp(prefix="audit-legacy-")
    dbpath = Path(tmp) / "app.db"
    con = sqlite3.connect(str(dbpath))
    con.executescript("""
        CREATE TABLE operators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(64) NOT NULL,
            token VARCHAR(64) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT 1,
            is_admin BOOLEAN NOT NULL DEFAULT 0,
            created_at FLOAT NOT NULL DEFAULT 0
        );
        INSERT INTO operators (name, token, enabled, is_admin, created_at)
        VALUES ('legacy', 'tok-legacy', 1, 1, 0);
    """)
    con.commit()
    con.close()
    inspect_db(str(dbpath), "路径3 老库（升级前）")

    code = CHILD_PREAMBLE + """
import os, sys
os.environ["AOHUANG_DATA_DIR"] = sys.argv[1]
from db.session import init_db
init_db()
"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, "-c", code, tmp], capture_output=True, env=env, cwd=str(SERVER),
    )
    print(f"\n路径3 init_db 升级老库 → exit {result.returncode}")
    if result.returncode != 0:
        print(result.stderr.decode("utf-8", "replace")[-800:])
    inspect_db(str(dbpath), "路径3 老库（升级后）")

    # 实测能否插入重复 short_id
    con = sqlite3.connect(str(dbpath))
    print("\n  实测插入两条相同 short_id：")
    for i in range(2):
        try:
            con.execute(
                "INSERT INTO operators (name, token, enabled, is_admin, created_at, short_id) "
                "VALUES (?,?,1,0,0,?)", (f"dup{i}", f"tok-dup{i}", "SAMETO"))
            con.commit()
            print(f"    第{i+1}条 short_id=SAMETO → 插入成功")
        except Exception as exc:
            print(f"    第{i+1}条 short_id=SAMETO → {type(exc).__name__}: {str(exc)[:90]}")
    con.close()


if __name__ == "__main__":
    path1_alembic()
    path2_create_all()
    path3_legacy_upgrade()
