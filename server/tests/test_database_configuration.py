from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from core.config import Settings
from db import session as session_module
from db.session import (
    BUSY_TIMEOUT_SECONDS,
    _create_engine,
    _engine_options,
    _sqlite_pragmas,
)


def test_database_url_defaults_to_local_sqlite(tmp_path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)

    assert settings.sqlalchemy_url.get_backend_name() == "sqlite"
    assert settings.sqlalchemy_url.database == str(tmp_path / "app.db")


def test_postgres_url_selects_psycopg_driver() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://user:password@db.example.test:5432/catalog",
    )

    url = settings.sqlalchemy_url
    assert url.drivername == "postgresql+psycopg"
    assert url.username == "user"
    assert url.password == "password"
    assert url.host == "db.example.test"


def test_engine_keeps_sqlite_connection_options_and_pragmas(tmp_path) -> None:
    engine = _create_engine(make_url(f"sqlite:///{tmp_path / 'engine.db'}"))
    try:
        assert engine.dialect.name == "sqlite"
        assert _engine_options(engine.url)["connect_args"] == {
            "check_same_thread": False,
            "timeout": BUSY_TIMEOUT_SECONDS,
        }
        assert event.contains(engine, "connect", _sqlite_pragmas)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
    finally:
        engine.dispose()


def test_postgres_engine_uses_postgres_dialect_without_sqlite_options() -> None:
    engine = _create_engine(
        make_url("postgresql+psycopg://user:password@localhost:5432/catalog")
    )
    try:
        assert engine.dialect.name == "postgresql"
        options = _engine_options(engine.url)
        # PostgreSQL 必须带连接池配置（2026-11-10 加）：默认只有 5+10=15 条，
        # 多人同时跑自动化时立刻打满，界面卡在「开始处理」。
        assert options["future"] is True
        assert options["pool_size"] >= 10
        assert options["max_overflow"] >= 10
        assert options["pool_pre_ping"] is True
        # 不能带 SQLite 专属参数
        assert "connect_args" not in options
        assert not event.contains(engine, "connect", _sqlite_pragmas)
    finally:
        engine.dispose()


class _ConnectionContext:
    def __init__(self, connection: object) -> None:
        self.connection = connection

    def __enter__(self) -> object:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        return None


def test_postgres_startup_accepts_only_current_migration_heads(monkeypatch) -> None:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    connection = object()
    fake_engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        connect=lambda: _ConnectionContext(connection),
    )
    monkeypatch.setattr(session_module, "engine", fake_engine)
    monkeypatch.setattr(
        ScriptDirectory, "from_config",
        lambda _config: SimpleNamespace(get_heads=lambda: ["head-a", "head-b"]),
    )
    monkeypatch.setattr(
        MigrationContext, "configure",
        lambda conn: SimpleNamespace(
            get_current_heads=lambda: ["head-b", "head-a"] if conn is connection else []
        ),
    )

    session_module._assert_postgresql_schema_current()


def test_postgres_startup_rejects_unapplied_heads_without_connecting_in_error(monkeypatch) -> None:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    fake_engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        connect=lambda: _ConnectionContext(object()),
    )
    monkeypatch.setattr(session_module, "engine", fake_engine)
    monkeypatch.setattr(
        ScriptDirectory, "from_config",
        lambda _config: SimpleNamespace(get_heads=lambda: ["expected-head"]),
    )
    monkeypatch.setattr(
        MigrationContext, "configure",
        lambda _conn: SimpleNamespace(get_current_heads=lambda: ["old-head"]),
    )

    with pytest.raises(RuntimeError, match="current=old-head, expected=expected-head"):
        session_module._assert_postgresql_schema_current()


def test_postgres_startup_reports_connection_failure_without_dsn(monkeypatch) -> None:
    from alembic.script import ScriptDirectory

    def fail_connect():
        raise RuntimeError("postgresql://admin:very-secret@db.internal/catalog unavailable")

    fake_engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"), connect=fail_connect,
    )
    monkeypatch.setattr(session_module, "engine", fake_engine)
    monkeypatch.setattr(
        ScriptDirectory, "from_config",
        lambda _config: SimpleNamespace(get_heads=lambda: ["head"]),
    )

    with pytest.raises(RuntimeError) as error:
        session_module._assert_postgresql_schema_current()
    assert "very-secret" not in str(error.value)
    assert "db.internal" not in str(error.value)


def test_postgres_init_only_checks_schema_and_seeds_settings(monkeypatch) -> None:
    fake_engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    calls: list[str] = []
    monkeypatch.setattr(session_module, "engine", fake_engine)
    from services import crypto

    monkeypatch.setattr(crypto, "validate_server_key", lambda: calls.append("key"))
    monkeypatch.setattr(session_module, "_assert_postgresql_schema_current",
                        lambda: calls.append("schema"))
    monkeypatch.setattr(session_module, "_seed_settings", lambda: calls.append("seed"))
    monkeypatch.setattr(session_module.Base.metadata, "create_all",
                        lambda *_args, **_kwargs: pytest.fail("must not create schema"))
    monkeypatch.setattr(session_module, "_add_missing_columns",
                        lambda: pytest.fail("must not patch schema"))

    session_module.init_db()

    assert calls == ["key", "schema", "seed"]


def test_postgres_init_stops_when_credential_key_is_missing(monkeypatch) -> None:
    fake_engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr(session_module, "engine", fake_engine)
    from services import crypto

    def fail_key_validation() -> None:
        raise crypto.CredentialError("key missing")

    monkeypatch.setattr(
        crypto,
        "validate_server_key",
        fail_key_validation,
    )
    monkeypatch.setattr(
        session_module,
        "_assert_postgresql_schema_current",
        lambda: pytest.fail("schema must not be checked without credential key"),
    )
    monkeypatch.setattr(
        session_module,
        "_seed_settings",
        lambda: pytest.fail("settings must not be seeded without credential key"),
    )

    with pytest.raises(crypto.CredentialError, match="key missing"):
        session_module.init_db()


def test_sqlite_init_keeps_create_patch_and_seed_path(monkeypatch) -> None:
    fake_engine = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    calls: list[str] = []
    monkeypatch.setattr(session_module, "engine", fake_engine)
    monkeypatch.setattr(session_module.Base.metadata, "create_all",
                        lambda *_args, **_kwargs: calls.append("create"))
    monkeypatch.setattr(session_module, "_add_missing_columns",
                        lambda: calls.append("patch"))
    monkeypatch.setattr(session_module, "_ensure_sqlite_operator_username_index",
                        lambda: calls.append("username_index"))
    monkeypatch.setattr(session_module, "_ensure_sqlite_operator_email_index",
                        lambda: calls.append("email_index"))
    monkeypatch.setattr(session_module, "_ensure_sqlite_schedule_ownership_indexes",
                        lambda: calls.append("schedule_indexes"))
    monkeypatch.setattr(session_module, "_ensure_sqlite_product_key_scope",
                        lambda: calls.append("product_key_scope"))
    monkeypatch.setattr(session_module, "_ensure_sqlite_publish_log_ownership",
                        lambda: calls.append("publish_log_ownership"))
    monkeypatch.setattr(session_module, "_ensure_sqlite_multi_account_bindings",
                        lambda: calls.append("multi_account_bindings"))
    monkeypatch.setattr(session_module, "_backfill_sqlite_account_bindings",
                        lambda: calls.append("backfill"))
    monkeypatch.setattr(session_module, "_ensure_sqlite_account_app_key_index",
                        lambda: calls.append("index"))
    monkeypatch.setattr(session_module, "_seed_settings", lambda: calls.append("seed"))

    session_module.init_db()

    assert calls == ["create", "patch", "username_index", "email_index", "schedule_indexes",
                     "product_key_scope",
                     "publish_log_ownership",
                     "multi_account_bindings",
                     "backfill", "index", "seed"]


def test_sqlite_init_rebuilds_legacy_publish_key_and_preserves_rows(
        monkeypatch, tmp_path) -> None:
    from sqlalchemy import create_engine, inspect, text

    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-publish-logs.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE publish_logs (
                id INTEGER PRIMARY KEY,
                account_id INTEGER,
                product_id INTEGER,
                schedule_id VARCHAR(32),
                warehouse_key VARCHAR(64) NOT NULL DEFAULT '',
                detail_id VARCHAR(32) NOT NULL DEFAULT '',
                shop_id VARCHAR(32) NOT NULL DEFAULT '',
                shop_name VARCHAR(255) NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                status VARCHAR(16) NOT NULL DEFAULT 'processing',
                reason TEXT NOT NULL DEFAULT '',
                platform_item_id VARCHAR(64),
                item_edit_url TEXT,
                mode VARCHAR(16) NOT NULL DEFAULT 'direct',
                batch_id VARCHAR(32) NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 1,
                detail JSON NOT NULL DEFAULT '{}',
                requested_at FLOAT NOT NULL DEFAULT 0,
                last_requested_at FLOAT,
                finished_at FLOAT,
                CONSTRAINT uq_publish_logs_request
                    UNIQUE (account_id, detail_id, shop_id)
            )
        """))
        connection.execute(text(
            "INSERT INTO publish_logs (id, account_id, detail_id, shop_id) "
            "VALUES (1, 7, '12345', '9')"
        ))

    monkeypatch.setattr(session_module, "engine", legacy_engine)
    monkeypatch.setattr(session_module, "_seed_settings", lambda: None)
    try:
        session_module.init_db()

        inspector = inspect(legacy_engine)
        assert not any(
            set(row.get("column_names") or []) == {"account_id", "detail_id", "shop_id"}
            for row in inspector.get_unique_constraints("publish_logs")
        )
        index_names = {row["name"] for row in inspector.get_indexes("publish_logs")}
        assert "uq_publish_logs_request_owner" in index_names
        assert "uq_publish_logs_request_unowned" in index_names

        with legacy_engine.begin() as connection:
            original = connection.execute(text(
                "SELECT id, account_id, detail_id, shop_id, operator_id "
                "FROM publish_logs WHERE id = 1"
            )).one()
            assert tuple(original) == (1, 7, "12345", "9", None)
            connection.execute(text(
                "INSERT INTO publish_logs "
                "(id, account_id, warehouse_key, detail_id, shop_id, shop_name, title, "
                "status, reason, mode, batch_id, attempts, detail, requested_at, operator_id) "
                "VALUES (2, 7, '', '12345', '9', '', '', 'success', '', 'direct', '', "
                "1, '{}', 0, 21), "
                "(3, 7, '', '12345', '9', '', '', 'success', '', 'direct', '', 1, '{}', 0, 22)"
            ))
            with pytest.raises(IntegrityError):
                connection.execute(text(
                    "INSERT INTO publish_logs "
                    "(id, account_id, warehouse_key, detail_id, shop_id, shop_name, title, "
                    "status, reason, mode, batch_id, attempts, detail, requested_at, operator_id) "
                    "VALUES (4, 7, '', '12345', '9', '', '', 'success', '', 'direct', '', "
                    "1, '{}', 0, 21)"
                ))
    finally:
        legacy_engine.dispose()


def test_sqlite_init_adds_schedule_ownership_indexes_to_legacy_schema(
        monkeypatch, tmp_path) -> None:
    from sqlalchemy import create_engine, inspect, text

    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-schedules.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE schedules (id VARCHAR(32) PRIMARY KEY, created_at FLOAT)"
        ))
        connection.execute(text(
            "CREATE TABLE schedule_logs (id INTEGER PRIMARY KEY, at FLOAT)"
        ))

    monkeypatch.setattr(session_module, "engine", legacy_engine)
    monkeypatch.setattr(session_module, "_seed_settings", lambda: None)
    try:
        session_module.init_db()

        schedule_indexes = {
            row["name"] for row in inspect(legacy_engine).get_indexes("schedules")
        }
        history_indexes = {
            row["name"] for row in inspect(legacy_engine).get_indexes("schedule_logs")
        }
        assert "ix_schedules_owner_created" in schedule_indexes
        assert "ix_schedule_logs_owner_at" in history_indexes
    finally:
        legacy_engine.dispose()


def _legacy_account_database(path, app_keys: tuple[str, ...]):
    from sqlalchemy import create_engine, text

    legacy_engine = create_engine(f"sqlite:///{path}")
    with legacy_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, app_key VARCHAR(256))"
        ))
        for index, app_key in enumerate(app_keys, start=1):
            connection.execute(
                text("INSERT INTO accounts (id, app_key) VALUES (:id, :app_key)"),
                {"id": index, "app_key": app_key},
            )
    return legacy_engine


def test_sqlite_init_adds_unique_app_key_index_to_legacy_schema(monkeypatch, tmp_path) -> None:
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import IntegrityError

    legacy_engine = _legacy_account_database(
        tmp_path / "legacy-accounts.db", ("existing-a", "existing-b", "", "")
    )
    monkeypatch.setattr(session_module, "engine", legacy_engine)
    monkeypatch.setattr(session_module, "_seed_settings", lambda: None)
    try:
        session_module.init_db()

        index_names = {row["name"] for row in inspect(legacy_engine).get_indexes("accounts")}
        assert "uq_accounts_app_key_nonempty" in index_names
        with legacy_engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO accounts (id, app_key) VALUES (10, 'existing-c')"
            ))
            with pytest.raises(IntegrityError):
                connection.execute(text(
                    "INSERT INTO accounts (id, app_key) VALUES (11, 'existing-a')"
                ))
            connection.execute(text(
                "INSERT INTO accounts (id, app_key) VALUES (12, '')"
            ))
    finally:
        legacy_engine.dispose()


def test_sqlite_init_reports_legacy_duplicate_keys_without_deleting_rows(
        monkeypatch, tmp_path) -> None:
    from sqlalchemy import inspect, text

    legacy_engine = _legacy_account_database(
        tmp_path / "legacy-duplicate-accounts.db",
        ("duplicate-secret-key", "duplicate-secret-key"),
    )
    monkeypatch.setattr(session_module, "engine", legacy_engine)
    monkeypatch.setattr(session_module, "_seed_settings", lambda: None)
    try:
        with pytest.raises(RuntimeError, match="重复的非空妙手 AppKey") as caught:
            session_module.init_db()

        assert "accounts.id=[1,2]" in str(caught.value)
        assert "duplicate-secret-key" not in str(caught.value)
        with legacy_engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT id, app_key FROM accounts ORDER BY id"
            )).all()
            assert rows == [(1, "duplicate-secret-key"), (2, "duplicate-secret-key")]
        index_names = {row["name"] for row in inspect(legacy_engine).get_indexes("accounts")}
        assert "uq_accounts_app_key_nonempty" not in index_names
    finally:
        legacy_engine.dispose()


def test_sqlite_init_adds_normalized_email_index_to_legacy_schema(
        monkeypatch, tmp_path) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError

    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-operator-email.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE operators (id INTEGER PRIMARY KEY, name VARCHAR(64), "
            "email VARCHAR(254))"
        ))
        connection.execute(text(
            "INSERT INTO operators (id, name, email) VALUES "
            "(1, 'teacher', ' Teacher@Example.com ')"
        ))

    monkeypatch.setattr(session_module, "engine", legacy_engine)
    monkeypatch.setattr(session_module, "_seed_settings", lambda: None)
    try:
        session_module.init_db()

        with legacy_engine.begin() as connection:
            index_names = set(connection.execute(text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'operators'"
            )).scalars())
            assert "uq_operators_email_normalized" in index_names
            with pytest.raises(IntegrityError):
                connection.execute(text(
                    "INSERT INTO operators (id, name, email) VALUES "
                    "(2, 'student', 'teacher@example.com')"
                ))
            connection.execute(text(
                "INSERT INTO operators (id, name, email) VALUES "
                "(3, 'other', 'other@example.com')"
            ))
    finally:
        legacy_engine.dispose()


def test_sqlite_init_restores_username_uniqueness_on_legacy_schema(
        monkeypatch, tmp_path) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError

    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-operator-username.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE operators (id INTEGER PRIMARY KEY, name VARCHAR(64), "
            "token VARCHAR(128))"
        ))
        connection.execute(text(
            "INSERT INTO operators (id, name, token) VALUES "
            "(1, 'local', 'token-1'), (2, 'student', 'token-2')"
        ))

    monkeypatch.setattr(session_module, "engine", legacy_engine)
    monkeypatch.setattr(session_module, "_seed_settings", lambda: None)
    try:
        session_module.init_db()

        with legacy_engine.connect() as connection:
            index_names = set(connection.execute(text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'operators'"
            )).scalars())
        assert "uq_operators_username" in index_names
        with legacy_engine.begin() as connection:
            connection.execute(text(
                "UPDATE operators SET username = 'student' WHERE id = 2"
            ))
            with pytest.raises(IntegrityError):
                connection.execute(text(
                    "UPDATE operators SET username = 'student' WHERE id = 1"
                ))
    finally:
        legacy_engine.dispose()


def test_sqlite_init_stops_on_duplicate_legacy_usernames_without_mutating_rows(
        monkeypatch, tmp_path) -> None:
    from sqlalchemy import create_engine, text

    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-duplicate-usernames.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE operators (id INTEGER PRIMARY KEY, name VARCHAR(64), "
            "username VARCHAR(64), token VARCHAR(128))"
        ))
        connection.execute(text(
            "INSERT INTO operators (id, name, username, token) VALUES "
            "(1, 'first', 'shared', 'token-1'), (2, 'second', 'shared', 'token-2')"
        ))

    monkeypatch.setattr(session_module, "engine", legacy_engine)
    monkeypatch.setattr(session_module, "_seed_settings", lambda: None)
    try:
        with pytest.raises(RuntimeError, match="重复用户名") as caught:
            session_module.init_db()

        assert "operators.id=[1,2]" in str(caught.value)
        with legacy_engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT id, username, token FROM operators ORDER BY id"
            )).all()
        assert rows == [(1, "shared", "token-1"), (2, "shared", "token-2")]
    finally:
        legacy_engine.dispose()


def test_sqlite_init_backfills_legacy_account_bindings_idempotently(
        monkeypatch, tmp_path) -> None:
    from sqlalchemy import create_engine, text

    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-bindings.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE operators (id INTEGER PRIMARY KEY)"
        ))
        connection.execute(text(
            "CREATE TABLE accounts ("
            "id INTEGER PRIMARY KEY, app_key VARCHAR(256), "
            "owner_operator_id INTEGER, bound_by_operator_id INTEGER, "
            "bound_at REAL)"
        ))
        connection.execute(text("INSERT INTO operators (id) VALUES (10), (20)"))
        connection.execute(text(
            "INSERT INTO accounts "
            "(id, app_key, owner_operator_id, bound_by_operator_id, bound_at) "
            "VALUES "
            "(1, 'legacy-a', 10, 20, 123.5), "
            "(2, 'legacy-b', 10, 20, 456.5), "
            "(3, 'legacy-c', 20, NULL, NULL)"
        ))

    monkeypatch.setattr(session_module, "engine", legacy_engine)
    monkeypatch.setattr(session_module, "_seed_settings", lambda: None)
    try:
        session_module.init_db()

        with legacy_engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT account_id, operator_id, bound_by_operator_id, bound_at "
                "FROM account_bindings ORDER BY operator_id"
            )).all()
        assert rows == [
            (1, 10, 20, 123.5),
            (2, 10, 20, 456.5),
            (3, 20, None, 0.0),
        ]

        # A second desktop launch must not duplicate or change existing bindings.
        session_module.init_db()
        with legacy_engine.connect() as connection:
            assert connection.execute(text(
                "SELECT COUNT(*) FROM account_bindings"
            )).scalar_one() == 3
    finally:
        legacy_engine.dispose()
