"""SQLAlchemy 引擎与会话。SQLite 本地优先，后续可换 Postgres。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from core.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

# 写锁等待上限（秒）。两个地方都要设，因为它们管的是不同的层：
#   · `connect_args.timeout` 是 Python sqlite3 模块自己的等锁超时；
#   · `PRAGMA busy_timeout` 是 SQLite C 层的等锁超时。
# 只设一个，另一个仍会先超时抛 "database is locked"。
#
# 为什么是 15 秒而不是更长/无限：
#   · 5 秒太短。WAL 下写是串行的，一个「认领 + 上游调用 + 落库」的请求
#     正常就要几秒，第二个人一点保存就撞上 5 秒上限，看到的是
#     "database is locked" —— 一句既不是他的错、也没告诉他怎么办的报错。
#   · 无限等待更糟。等不到锁时界面会一直转圈，人不知道是该继续等还是刷新，
#     最后只能杀进程；而 15 秒后失败至少给出一句能行动的提示。
#   · 15 秒够覆盖正常的写冲突（另一件事马上就会提交完），又不至于让人以为死机。
BUSY_TIMEOUT_SECONDS = 15.0

def _sqlite_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    # 与上面的 connect_args.timeout 保持一致，理由见 BUSY_TIMEOUT_SECONDS 注释
    cursor.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SECONDS * 1000)}")
    cursor.close()


def _engine_options(url: URL) -> dict[str, object]:
    options: dict[str, object] = {"future": True}
    if url.get_backend_name() == "sqlite":
        options["connect_args"] = {
            "check_same_thread": False,
            "timeout": BUSY_TIMEOUT_SECONDS,
        }
    return options


def _create_engine(url: URL) -> Engine:
    options = _engine_options(url)
    database_engine = create_engine(url, **options)
    if database_engine.dialect.name == "sqlite":
        event.listen(database_engine, "connect", _sqlite_pragmas)
    return database_engine


engine = _create_engine(_settings.sqlalchemy_url)


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _add_missing_columns() -> None:
    """给已存在的表补上新增的列。

    `create_all` 只建表、不改表：老库升级后新列不存在，一查就报
    `no such column`。这里对比模型定义与实际表结构，缺哪列补哪列。
    只用 SQLite 支持的 `ALTER TABLE ... ADD COLUMN`（带默认值）。

    ── 与 Alembic 的分工（改这里之前先看 docs/migrations.md）──────────────
    两条路各管一段，**不是二选一**：

      · **Alembic**（server/migrations/）是新库从零建表的正路，
        也是「改了列类型 / 删列 / 改约束」的唯一正路 ——
        这些 `ALTER TABLE ADD COLUMN` 根本做不到。

      · **本函数**是老用户库的兜底。现有用户的库里已经有 355 件真实商品，
        他们双击 exe 升级时不会、也不该手动跑 alembic；`init_db()` 里的
        `create_all` 补不了列，只有这个函数能把他们从 `no such column`
        里救出来。

    因此：**新加一列时，Alembic 迁移和这里的模型定义都要有**。
    模型改了（列会自动出现在 metadata 里）本函数自然就兜住了；但如果你只改
    模型不写迁移，新库从零建（走 alembic）就会缺这列。
    反过来，只写迁移不改模型，autogenerate 下次会把这条迁移「撤销」掉。

    注意本函数只做 ADD COLUMN：类型变更、删列、加唯一约束、改外键都不会
    被兜住，那几种情况必须写 Alembic 迁移并在发版说明里要求用户升级。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                try:
                    ddl += column.type.compile(dialect=engine.dialect)
                except Exception:  # noqa: BLE001 - 罕见类型退回 TEXT
                    ddl = (f'ALTER TABLE "{table.name}" ADD COLUMN '
                           f'"{column.name}" TEXT')
                default = column.default
                if default is not None and getattr(default, "is_scalar", False):
                    value = default.arg
                    if isinstance(value, bool):
                        ddl += f" DEFAULT {1 if value else 0}"
                    elif isinstance(value, (int, float)):
                        ddl += f" DEFAULT {value}"
                    elif isinstance(value, str):
                        escaped = value.replace("'", "''")
                        ddl += f" DEFAULT '{escaped}'"
                connection.execute(text(ddl))


def _ensure_sqlite_account_app_key_index() -> None:
    """Add the partial unique AppKey index to legacy desktop databases safely.

    Existing databases may contain duplicate non-empty keys. Preserve every row
    and stop startup with an actionable error instead of silently choosing one.
    """
    from sqlalchemy import text

    with engine.begin() as connection:
        duplicates = connection.execute(text(
            "SELECT group_concat(id, ',') FROM accounts "
            "WHERE app_key IS NOT NULL AND app_key <> '' "
            "GROUP BY app_key HAVING COUNT(*) > 1 ORDER BY MIN(id)"
        )).scalars().all()
        if duplicates:
            details = "; ".join(
                f"accounts.id=[{','.join(str(int(value)) for value in ids.split(','))}]"
                for ids in duplicates
            )
            raise RuntimeError(
                "无法升级：accounts 中存在重复的非空妙手 AppKey，未修改数据。"
                "请人工核对并处理以下账号后重试；应用不会自动合并或删除凭据："
                f" {details}"
            )

        connection.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_accounts_app_key_nonempty" '
            'ON "accounts" ("app_key") WHERE "app_key" <> ""'
        ))


def _ensure_sqlite_schedule_ownership_indexes() -> None:
    """Create ownership indexes after legacy SQLite tables gain their columns."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    indexes = (
        ("schedules", "ix_schedules_owner_created",
         "owner_operator_id, created_at"),
        ("schedule_logs", "ix_schedule_logs_owner_at",
         "owner_operator_id, at"),
    )
    with engine.begin() as connection:
        for table, index, columns in indexes:
            if table not in tables:
                continue
            names = {column["name"] for column in inspector.get_columns(table)}
            expected = {part.strip() for part in columns.split(",")}
            if expected.issubset(names):
                connection.execute(text(
                    f'CREATE INDEX IF NOT EXISTS "{index}" ON "{table}" ({columns})'
                ))


def _ensure_sqlite_publish_log_ownership() -> None:
    """Replace the legacy account-wide publish key without losing ledger rows.

    Desktop SQLite databases are upgraded at startup and do not run Alembic.
    The old table-level unique constraint cannot be dropped in place, so rebuild
    only that table using the current model, copy every modeled column, and then
    recreate its indexes. Unknown columns abort the upgrade before any change.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateTable

    inspector = inspect(engine)
    if "publish_logs" not in inspector.get_table_names():
        return

    from models.publish_log import PublishLog

    model_table = PublishLog.__table__
    existing_columns = {column["name"] for column in inspector.get_columns("publish_logs")}
    model_columns = {column.name for column in model_table.columns}
    unknown_columns = existing_columns - model_columns
    if unknown_columns:
        raise RuntimeError(
            "无法升级 publish_logs：存在当前版本不认识的列，未修改数据："
            + ", ".join(sorted(unknown_columns))
        )

    old_key = {"account_id", "detail_id", "shop_id"}
    has_legacy_key = any(
        set(constraint.get("column_names") or []) == old_key
        for constraint in inspector.get_unique_constraints("publish_logs")
    )
    if has_legacy_key:
        # Keep foreign-key targets available while SQLAlchemy compiles the
        # temporary table. The temporary metadata is removed immediately after
        # the rebuild so it cannot affect later model operations.
        temporary = model_table.to_metadata(
            Base.metadata, name="publish_logs_owner_upgrade")
        column_names = [column.name for column in model_table.columns
                        if column.name in existing_columns]
        quoted = ", ".join(f'"{name}"' for name in column_names)
        try:
            with engine.begin() as connection:
                connection.execute(CreateTable(temporary))
                connection.execute(text(
                    f'INSERT INTO "publish_logs_owner_upgrade" ({quoted}) '
                    f'SELECT {quoted} FROM "publish_logs"'
                ))
                connection.execute(text('DROP TABLE "publish_logs"'))
                connection.execute(text(
                    'ALTER TABLE "publish_logs_owner_upgrade" RENAME TO "publish_logs"'
                ))
        finally:
            Base.metadata.remove(temporary)

    with engine.begin() as connection:
        for index in model_table.indexes:
            index.create(connection, checkfirst=True)


def _ensure_sqlite_product_key_scope() -> None:
    """Remove the legacy global ``products.warehouse_key`` uniqueness.

    Older desktop databases were created from a model where ``warehouse_key``
    was globally unique.  The key is only stable inside one Miaoshou account,
    so keeping that constraint prevents two accounts from storing the same
    detail/shop pair.  SQLite cannot drop an inline UNIQUE constraint; rebuild
    the table from the current model while preserving every modeled column,
    index and foreign key.  Unknown columns abort before any DDL is issued.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateTable

    inspector = inspect(engine)
    if "products" not in inspector.get_table_names():
        return

    from models.product import Product

    model_table = Product.__table__
    existing_columns = {column["name"] for column in inspector.get_columns("products")}
    model_columns = {column.name for column in model_table.columns}
    unknown_columns = existing_columns - model_columns
    if unknown_columns:
        raise RuntimeError(
            "无法升级 products：存在当前版本不认识的列，未修改数据："
            + ", ".join(sorted(unknown_columns))
        )

    global_key_constraint = any(
        set(constraint.get("column_names") or []) == {"warehouse_key"}
        for constraint in inspector.get_unique_constraints("products")
    )
    global_key_index = any(
        index.get("unique")
        and set(index.get("column_names") or []) == {"warehouse_key"}
        for index in inspector.get_indexes("products")
    )
    if not global_key_constraint and not global_key_index:
        return

    with engine.connect() as connection:
        # SQL-backed indexes are not represented by table constraints. Preserve
        # all of them except a standalone global unique index on warehouse_key;
        # SQLite's anonymous autoindexes are recreated from the model table.
        unique_index_names = {
            str(row[1])
            for row in connection.exec_driver_sql(
                'PRAGMA index_list("products")').all()
            if bool(row[2])
        }
        preserved_indexes: list[str] = []
        preserved_index_names: set[str] = set()
        for name, ddl in connection.execute(text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='products' AND sql IS NOT NULL"
        )).all():
            index_name = str(name)
            if index_name in unique_index_names:
                escaped = index_name.replace('"', '""')
                columns = [row[2] for row in connection.exec_driver_sql(
                    f'PRAGMA index_info("{escaped}")').all()]
                if columns == ["warehouse_key"]:
                    continue
            if ddl:
                preserved_indexes.append(str(ddl))
                preserved_index_names.add(index_name)

        preserved_triggers = [
            str(row[0]) for row in connection.execute(text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name='products' AND sql IS NOT NULL"
            )).all() if row[0]
        ]
        foreign_keys_before = connection.exec_driver_sql(
            'PRAGMA foreign_key_check').all()

    # Foreign-key tables reference products.id.  SQLite cannot drop a
    # referenced table while enforcement is on; toggle it outside the DDL
    # transaction, then restore it in a finally block.  The table rebuild and
    # rename themselves remain atomic and roll back together on any failure.
    temporary = model_table.to_metadata(
        Base.metadata, name="products_key_scope_upgrade")
    column_names = [column.name for column in model_table.columns
                    if column.name in existing_columns]
    quoted = ", ".join(f'"{name}"' for name in column_names)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            try:
                with connection.begin():
                    connection.execute(CreateTable(temporary))
                    connection.execute(text(
                        f'INSERT INTO "products_key_scope_upgrade" ({quoted}) '
                        f'SELECT {quoted} FROM "products"'
                    ))
                    connection.execute(text('DROP TABLE "products"'))
                    connection.execute(text(
                        'ALTER TABLE "products_key_scope_upgrade" RENAME TO "products"'
                    ))
                    for ddl in preserved_indexes:
                        connection.exec_driver_sql(ddl)
                    for ddl in preserved_triggers:
                        connection.exec_driver_sql(ddl)
                    for index in model_table.indexes:
                        if index.name not in preserved_index_names:
                            index.create(connection, checkfirst=True)
                    foreign_keys_after = connection.exec_driver_sql(
                        'PRAGMA foreign_key_check').all()
                    if foreign_keys_after != foreign_keys_before:
                        raise RuntimeError(
                            "无法升级 products：重建会改变现有外键关系，已回滚"
                        )
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
    finally:
        Base.metadata.remove(temporary)


def _assert_sqlite_operator_emails_unique() -> None:
    """Fail before index creation if legacy mailboxes collide case-insensitively."""
    from sqlalchemy import inspect, text

    try:
        inspector = inspect(engine)
    except Exception:  # pragma: no cover - test doubles / unavailable engine
        return
    if "operators" not in inspector.get_table_names():
        return
    operator_columns = {column["name"] for column in inspector.get_columns("operators")}
    if "email" not in operator_columns:
        return
    with engine.connect() as connection:
        duplicates = connection.execute(text(
            "SELECT group_concat(id, ',') FROM operators "
            "WHERE email IS NOT NULL AND trim(email) <> '' "
            "GROUP BY lower(trim(email)) HAVING COUNT(*) > 1 ORDER BY MIN(id)"
        )).scalars().all()
    if duplicates:
        ids = "; ".join(
            f"operators.id=[{','.join(str(int(value)) for value in group.split(','))}]"
            for group in duplicates
        )
        raise RuntimeError(
            "无法升级：operators 中存在重复的邮箱（忽略大小写与首尾空格后）。"
            "为避免错误登录或找回密码，应用没有修改数据；请人工核对以下账号 ID 后重试："
            f" {ids}"
        )


def _ensure_sqlite_operator_email_index() -> None:
    """Create the normalized-email index missing from pre-existing SQLite tables."""
    from sqlalchemy import inspect, text

    try:
        if "operators" not in inspect(engine).get_table_names():
            return
    except Exception:  # pragma: no cover - test doubles / unavailable engine
        return
    _assert_sqlite_operator_emails_unique()
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_operators_email_normalized" '
            'ON "operators" (lower(trim(email))) '
            'WHERE email IS NOT NULL AND trim(email) <> \'\''
        ))


def _ensure_sqlite_operator_username_index() -> None:
    """Restore username uniqueness on desktop databases upgraded without Alembic.

    ``_add_missing_columns`` can add ``username`` to a legacy ``operators`` table,
    but cannot reproduce the unique constraint from the Alembic migration. Fail
    without changing account rows if duplicates already exist.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "operators" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("operators")}
    if "username" not in columns:
        return

    for index in inspector.get_indexes("operators"):
        if index.get("unique") and index.get("column_names") == ["username"]:
            return
    for constraint in inspector.get_unique_constraints("operators"):
        if constraint.get("column_names") == ["username"]:
            return

    with engine.connect() as connection:
        duplicates = connection.execute(text(
            "SELECT group_concat(id, ',') FROM operators "
            "WHERE username IS NOT NULL "
            "GROUP BY username HAVING COUNT(*) > 1 ORDER BY MIN(id)"
        )).scalars().all()
    if duplicates:
        details = "; ".join(
            f"operators.id=[{','.join(str(int(value)) for value in ids.split(','))}]"
            for ids in duplicates
        )
        raise RuntimeError(
            "无法升级：operators 中存在重复用户名，未修改账号数据。"
            "请人工核对以下账号 ID 后重试： " + details
        )

    with engine.begin() as connection:
        connection.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_operators_username" '
            'ON "operators" ("username")'
        ))


def _backfill_sqlite_account_bindings() -> None:
    """将旧库里的账号负责人迁移为显式的共享账号绑定。

    SQLite 启动路径不会执行 Alembic。新版 ``account_bindings`` 表虽然会由
    ``create_all`` 创建，但老库里的 ``accounts.owner_operator_id`` 不会自动
    复制进去，导致原学员登录后看不到自己已有的妙手账号。只补没有绑定的
    操作员，保证重复启动幂等；同一旧操作员若意外拥有多个账号，按账号 ID
    最小的记录保留，符合「一个软件用户只能绑定一个妙手账号」的现行规则。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "accounts" not in tables or "account_bindings" not in tables:
        return
    account_columns = {column["name"] for column in inspector.get_columns("accounts")}
    if "owner_operator_id" not in account_columns:
        return

    with engine.begin() as connection:
        connection.execute(text(
            """
            INSERT INTO account_bindings
                (account_id, operator_id, bound_by_operator_id, bound_at)
            SELECT a.id, a.owner_operator_id, a.bound_by_operator_id,
                   COALESCE(a.bound_at, 0.0)
            FROM accounts AS a
            JOIN (
                SELECT owner_operator_id, MIN(id) AS account_id
                FROM accounts
                WHERE owner_operator_id IS NOT NULL
                GROUP BY owner_operator_id
            ) AS first_account
              ON first_account.account_id = a.id
            WHERE a.owner_operator_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM account_bindings AS existing
                  WHERE existing.operator_id = a.owner_operator_id
              )
            ON CONFLICT(operator_id) DO NOTHING
            """
        ))
        # Keep duplicate legacy credentials and their old owner id intact, but
        # do not let them re-enter the compatibility fallback after an explicit
        # binding is removed.
        connection.execute(text(
            """
            UPDATE accounts
            SET legacy_owner_fallback_disabled = 1
            WHERE owner_operator_id IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM account_bindings AS binding
                  WHERE binding.operator_id = accounts.owner_operator_id
                    AND binding.account_id <> accounts.id
              )
            """
        ))


def init_db() -> None:
    """建表 + 补列 + 写入默认设置。

    SQLite 启动路径**不走 alembic**：打包后的 sidecar 里没有迁移目录，
    而且老用户库的表已经存在，`upgrade head` 会因「table already exists」
    直接失败，让应用起不来。所以这里仍是 `create_all` + `_add_missing_columns`
    的组合。PostgreSQL 只校验连接与已应用的迁移 heads，不在应用启动时改 schema。
    """
    import importlib

    if engine.dialect.name == "postgresql":
        from services.crypto import validate_server_key

        validate_server_key()
        _assert_postgresql_schema_current()
        _seed_settings()
        return

    # 导入 models 包即把所有模型注册到 Base.metadata（避免逐文件 import）
    importlib.import_module("models")
    _assert_sqlite_operator_emails_unique()
    Base.metadata.create_all(engine)
    _add_missing_columns()
    _ensure_sqlite_operator_username_index()
    _ensure_sqlite_operator_email_index()
    _ensure_sqlite_schedule_ownership_indexes()
    _ensure_sqlite_product_key_scope()
    _ensure_sqlite_publish_log_ownership()
    _backfill_sqlite_account_bindings()
    _ensure_sqlite_account_app_key_index()
    _seed_settings()


def _assert_postgresql_schema_current() -> None:
    """Require a reachable PostgreSQL database whose Alembic heads match this build."""
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    expected = set(ScriptDirectory.from_config(config).get_heads())
    try:
        with engine.connect() as connection:
            current = set(MigrationContext.configure(connection).get_current_heads())
    except Exception as exc:  # noqa: BLE001 - do not expose the configured DSN
        raise RuntimeError(
            f"PostgreSQL startup check could not connect/read migration state "
            f"({type(exc).__name__})") from None

    if current != expected:
        current_text = ", ".join(sorted(current)) or "none"
        expected_text = ", ".join(sorted(expected)) or "none"
        raise RuntimeError(
            "PostgreSQL schema is not at this application's Alembic head; "
            f"current={current_text}, expected={expected_text}. "
            "Run the reviewed database migration command before starting the app.")


def _seed_settings() -> None:
    from models.setting import DEFAULT_SETTINGS, Setting

    with session_scope() as db:
        existing = {row.key for row in db.query(Setting).all()}
        for key, value in DEFAULT_SETTINGS.items():
            if key in existing:
                continue
            db.add(Setting(key=key, value=value))
        _fix_workflow_defaults(db)
        db.commit()


def _fix_workflow_defaults(db) -> None:
    """把存量库里写错的三个工作流默认值纠正过来。

    只补缺失键是不够的：这三个键在旧版里已经写进了库，值是错的
    （增量 0.01 而非 0.1、尺寸键是 length/width/height、尺码图为空），
    跳过就永远错。这里只改「明显是旧默认值」的那种值，用户自己填过的不动。
    """
    from models.setting import WORKFLOW_DEFAULTS_KEY, Setting

    row = db.query(Setting).filter(Setting.key == WORKFLOW_DEFAULTS_KEY).one_or_none()
    if row is None or not isinstance(row.value, dict):
        return
    value = dict(row.value)
    changed = False

    if value.get("weightIncrement") in (0.01, "0.01", None):
        value["weightIncrement"] = 0.1
        changed = True

    dimensions = value.get("packageDimensions")
    if not isinstance(dimensions, dict) or set(dimensions) == {"length", "width", "height"}:
        value["packageDimensions"] = {
            "packageLength": 30, "packageWidth": 20, "packageHeight": 10}
        changed = True

    if value.get("sizeChartUrl") == "":
        from models.setting import DEFAULT_SETTINGS

        value["sizeChartUrl"] = DEFAULT_SETTINGS[WORKFLOW_DEFAULTS_KEY]["sizeChartUrl"]
        changed = True

    if changed:
        row.value = value
