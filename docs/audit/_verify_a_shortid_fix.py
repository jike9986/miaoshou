"""验证 A-P0-1 的修复方案：short_id 唯一约束怎么补才对，以及能不能补上。

## 为什么单独验证修法

A-P0-1 说「short_id 有模型无迁移」。但「补一条迁移」不等于修好：

  · 老用户的桌面库**不会跑 Alembic**（双击 exe 升级时不跑），
    只靠 `_add_missing_columns` 补列 → 约束永远缺
  · 所以必须**同时**补迁移（新库/PostgreSQL 正路）和补偿函数（老 SQLite 库）
  · 而且补偿函数必须处理「库里已经有重复 short_id」的情况 ——
    直接 CREATE UNIQUE INDEX 会抛 IntegrityError，要让升级可诊断而不是崩

本脚本用真实库实测三条路径，并验证补偿函数在「有重复数据」时的行为。

## 顺带验证一个更细的点

`short_id` 是 `String(8), unique=True` 且 **nullable**。
SQLite 的唯一索引对 NULL 是「允许多个 NULL」的 —— 所以：
  · 未生成 short_id 的老记录（NULL）不会互相冲突 ✅
  · 但只要两条记录有相同的非 NULL short_id，就必须被拦住

如果实现成 `WHERE short_id IS NOT NULL` 的部分唯一索引，行为正好符合这个语义。

用法：cd miaoshou/server && python -X utf8 ../docs/audit/_verify_a_shortid_fix.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "server"


def build_legacy_db(path: Path, with_duplicates: bool) -> None:
    """造一个「有 short_id 列但没有唯一约束」的老库。

    这正是老用户双击 exe 升级后的状态：_add_missing_columns 补了列，
    但 CREATE TABLE 语句里没有 UNIQUE，索引也不存在。
    """
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE operators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(64) NOT NULL,
            token VARCHAR(64) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT 1,
            is_admin BOOLEAN NOT NULL DEFAULT 0,
            role VARCHAR(16) NOT NULL DEFAULT 'member',
            created_at FLOAT NOT NULL DEFAULT 0,
            short_id VARCHAR(8)
        );
    """)
    rows = [("op1", "t1", "AAAAAA"), ("op2", "t2", "BBBBBB")]
    if with_duplicates:
        rows.append(("op3", "t3", "AAAAAA"))     # 故意重复
    for name, token, sid in rows:
        con.execute(
            "INSERT INTO operators (name, token, short_id) VALUES (?,?,?)",
            (name, token, sid))
    con.commit()
    con.close()


def has_unique(engine, table: str, column: str) -> bool:
    from sqlalchemy import inspect
    ins = inspect(engine)
    for idx in ins.get_indexes(table):
        if idx.get("unique") and column in (idx.get("column_names") or []):
            return True
    for uq in ins.get_unique_constraints(table):
        if column in (uq.get("column_names") or []):
            return True
    return False


def run_ensure(engine, *, raise_on_duplicate: bool = True) -> tuple[bool, str]:
    """模拟补偿函数的行为（与 _ensure_sqlite_operator_username_index 同构）。

    返回 (是否成功, 说明)。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "operators" not in inspector.get_table_names():
        return False, "operators 表不存在"
    columns = {c["name"] for c in inspector.get_columns("operators")}
    if "short_id" not in columns:
        return False, "short_id 列不存在（_add_missing_columns 还没补）"

    if has_unique(engine, "operators", "short_id"):
        return True, "唯一约束已存在，跳过"

    # 关键：先查重再建索引。直接建会抛 IntegrityError，升级中断且难排查。
    with engine.connect() as connection:
        duplicates = connection.execute(text(
            "SELECT short_id FROM operators "
            "WHERE short_id IS NOT NULL AND trim(short_id) <> '' "
            "GROUP BY short_id HAVING COUNT(*) > 1"
        )).scalars().all()
    if duplicates and raise_on_duplicate:
        return False, f"发现重复 short_id: {list(duplicates)}，拒绝建索引（需人工处理）"

    with engine.begin() as connection:
        connection.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_operators_short_id" '
            'ON "operators" (short_id) '
            "WHERE short_id IS NOT NULL AND trim(short_id) <> ''"
        ))
    return True, "已创建部分唯一索引 uq_operators_short_id"


def main() -> None:
    from sqlalchemy import create_engine, text

    print("=== 场景 1：老库有 short_id 列、无唯一约束、数据干净 ===")
    tmp1 = Path(tempfile.mkdtemp(prefix="audit-shortid-fix1-")) / "app.db"
    build_legacy_db(tmp1, with_duplicates=False)
    eng1 = create_engine(f"sqlite:///{tmp1}")
    print(f"  修复前有唯一约束: {has_unique(eng1, 'operators', 'short_id')}")
    ok, msg = run_ensure(eng1)
    print(f"  补偿函数: {ok} — {msg}")
    print(f"  修复后有唯一约束: {has_unique(eng1, 'operators', 'short_id')}")

    # 实测：重复插入必须被拦，多个 NULL 必须仍被允许
    with eng1.begin() as conn:
        try:
            conn.execute(text(
                "INSERT INTO operators (name,token,short_id) VALUES ('dup','t9','AAAAAA')"))
            print("  插入重复 AAAAAA → 竟然成功 ✗（约束没生效）")
        except Exception as exc:
            print(f"  插入重复 AAAAAA → 被拦住 ✓ ({type(exc).__name__})")
        try:
            conn.execute(text(
                "INSERT INTO operators (name,token,short_id) VALUES ('n1','t10',NULL)"))
            conn.execute(text(
                "INSERT INTO operators (name,token,short_id) VALUES ('n2','t11',NULL)"))
            print("  插入两条 NULL short_id → 成功 ✓（部分索引语义正确）")
        except Exception as exc:
            print(f"  插入两条 NULL → 被拦 ✗ ({type(exc).__name__})")

    print("\n=== 场景 2：老库已有重复 short_id（真实世界会有）===")
    tmp2 = Path(tempfile.mkdtemp(prefix="audit-shortid-fix2-")) / "app.db"
    build_legacy_db(tmp2, with_duplicates=True)
    eng2 = create_engine(f"sqlite:///{tmp2}")
    ok2, msg2 = run_ensure(eng2)
    print(f"  补偿函数: {ok2} — {msg2}")
    print(f"  修复后有唯一约束: {has_unique(eng2, 'operators', 'short_id')}")
    if not ok2:
        print("  ✓ 正确行为：报告重复而不是让升级崩掉（IntegrityError → 可诊断）")

    print("\n=== 场景 3：重复数据先修掉，再建索引 ===")
    with eng2.begin() as conn:
        # 把重复的那条改成新值（真实修法：生成新的 short_id）
        conn.execute(text(
            "UPDATE operators SET short_id='CCCCCC' "
            "WHERE id=(SELECT MAX(id) FROM operators WHERE short_id='AAAAAA')"))
    ok3, msg3 = run_ensure(eng2)
    print(f"  清理后补偿函数: {ok3} — {msg3}")
    print(f"  修复后有唯一约束: {has_unique(eng2, 'operators', 'short_id')}")

    print("\n=== 结论 ===")
    print("  修复需要三件套，缺一不可：")
    print("    ① Alembic 迁移 —— 补 short_id 列 + 唯一约束（新库 / PostgreSQL 正路）")
    print("    ② _ensure_sqlite_short_id_index() 补偿函数 —— 老 SQLite 库（不跑 Alembic）")
    print("    ③ 先查重再建索引 —— 有重复时明确报错，而不是 IntegrityError 崩升级")
    print()
    print("  关键细节：必须用部分唯一索引 WHERE short_id IS NOT NULL，")
    print("  否则历史 NULL 记录（没生成过 short_id 的老账号）会互相冲突。")
    print("  这一点和现有的 _ensure_sqlite_operator_email_index 完全同构，照着写即可。")


if __name__ == "__main__":
    main()
