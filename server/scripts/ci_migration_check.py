"""CI 用的迁移冒烟检查：临时库 upgrade head → 报表 → downgrade base → 断言清空。

为什么单独写成脚本而不是在 workflow 里内联 python：
  1. 内联要嵌 heredoc，YAML 缩进 + heredoc 终止符一旦对不齐就是难查的语法错；
  2. 同样的逻辑本地要能一条命令跑，不能只在 CI 里存在；
  3. 退出码要能表达「建了几张表」「回退是否干净」，内联写不出来。

用法（在 server/ 下，或从仓库根 `python server/scripts/ci_migration_check.py`）：
    python scripts/ci_migration_check.py

不读也不写真实 app.db：AOHUANG_DATA_DIR 在导入任何项目代码之前就被改成
一次性临时目录。CI 里即使 job 级环境变量指向别处，这里也会覆盖掉。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent

# 必须在 import core.config / db.session 之前设置：
# db/session.py 在模块导入时就把 engine 绑到了当时的 data_dir。
_TMP = Path(tempfile.mkdtemp(prefix="aohuang-ci-mig-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP)
os.environ["AOHUANG_DATABASE_URL"] = ""
sys.path.insert(0, str(SERVER_DIR))

# 这两行必须在上面设置完 AOHUANG_DATA_DIR 之后：db/session.py 在模块导入时
# 就把 engine 绑到了当时的 data_dir，之后再改环境变量不会生效。
import sqlalchemy as sa  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

# 期望值**不硬编码表数量**：直接读 Base.metadata，模型加了表这里自动跟上。
# 硬编码的后果是「另一个 agent 加了 2 个模型 → CI 红」，而真正该被发现的是
# 「迁移漏建了这些表」——那由下面的集合比对负责。
# 之所以要在导入 models 之后取：models/__init__.py 导入即注册。
import models  # noqa: E402,F401  —— 副作用导入
from core.config import get_settings  # noqa: E402
from db.session import Base  # noqa: E402

EXPECTED_TABLES = frozenset(Base.metadata.tables)


def _utf8_stdio() -> None:
    """Windows 控制台默认 GBK，中文报告会变乱码；CI 是 UTF-8，不受影响。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")


def _business_tables(db_path: Path) -> list[str]:
    """业务表（排除 sqlite 内部表与 alembic_version）。"""
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        return [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name")]


def _alembic_config() -> Config:
    ini = Config(str(SERVER_DIR / "alembic.ini"))
    ini.set_main_option("script_location", str(SERVER_DIR / "migrations"))
    return ini


def main() -> int:
    _utf8_stdio()
    db_path = get_settings().db_path
    print(f"[迁移检查] 临时数据目录：{_TMP}")
    print(f"[迁移检查] 目标库：{db_path}")

    ini = _alembic_config()
    failures: list[str] = []

    # ---- upgrade head ----
    command.upgrade(ini, "head")
    tables = set(_business_tables(db_path))
    print(f"[1] upgrade head 后业务表数量：{len(tables)}（metadata 里 {len(EXPECTED_TABLES)} 张）")
    print("    " + " / ".join(sorted(tables)))
    missing = EXPECTED_TABLES - tables
    extra = tables - EXPECTED_TABLES
    if missing:
        failures.append(f"迁移漏建了这些表（模型里有、库里没有）：{sorted(missing)}")
    if extra:
        failures.append(f"库里多出模型里没有的表（迁移该删？）：{sorted(extra)}")

    # ---- 索引 / 外键计数（结构没漏建）----
    # 期望值从 metadata 派生：模型里 Index() 的数量 + UNIQUE 约束的数量。
    # 不硬编码数字，模型变了自动跟上。
    with sqlite3.connect(db_path) as conn:
        explicit_indexes = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
        auto_indexes = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'sqlite_autoindex_%' AND tbl_name != 'alembic_version'"
        ).fetchone()[0]
        fk_count = sum(len(conn.execute(f'PRAGMA foreign_key_list("{t}")').fetchall())
                       for t in tables)
    model_indexes = sum(len(t.indexes) for t in Base.metadata.tables.values())
    # UNIQUE 在 SQLite 里体现为 sqlite_autoindex_<表>_<n>。但它同时也会为
    # **字符串主键**建 autoindex（INTEGER PRIMARY KEY 是 rowid 别名，不建）。
    # 所以期望值 = UniqueConstraint 数量 + 非整数主键的表数量。
    # 注意别把 `unique=True` 的列单独再数一遍：SQLAlchemy 会把它表示成一条
    # UniqueConstraint 放进 table.constraints，重复计数会多算 6 个。
    model_uniques = sum(
        sum(1 for c in t.constraints if c.__class__.__name__ == "UniqueConstraint")
        for t in Base.metadata.tables.values())
    model_text_pks = sum(
        1 for t in Base.metadata.tables.values()
        if t.primary_key.columns and not any(
            isinstance(c.type, sa.Integer) for c in t.primary_key.columns))
    model_fks = sum(len(t.foreign_key_constraints) for t in Base.metadata.tables.values())
    expected_autoindexes = model_uniques + model_text_pks
    print(f"[2] 索引：库里显式 {explicit_indexes} / 模型 Index {model_indexes}；"
          f"autoindex {auto_indexes} / 期望 {expected_autoindexes}"
          f"（唯一约束 {model_uniques} + 字符串主键 {model_text_pks}）")
    print(f"    外键：库里 {fk_count} / 模型 {model_fks}")
    if explicit_indexes != model_indexes:
        failures.append(f"显式索引 {explicit_indexes} != 模型 Index {model_indexes}，迁移漏建索引")
    if auto_indexes != expected_autoindexes:
        failures.append(
            f"autoindex {auto_indexes} != 期望 {expected_autoindexes}，迁移漏建唯一约束/主键")
    if fk_count != model_fks:
        failures.append(f"外键 {fk_count} != 模型 {model_fks}，迁移漏建外键")

    # ---- downgrade base ----
    command.downgrade(ini, "base")
    remaining = _business_tables(db_path)
    print(f"[3] downgrade base 后剩余业务表：{len(remaining)}（应为 0）")
    if remaining:
        failures.append(f"downgrade base 未清空：{remaining}")

    print()
    if failures:
        print("[结论] 迁移检查未通过：", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"[结论] 迁移检查通过（upgrade 建 {len(tables)} 表 / 索引与外键齐全 / downgrade 清空）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
