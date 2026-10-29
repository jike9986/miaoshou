from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url

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
        assert _engine_options(engine.url) == {"future": True}
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
    monkeypatch.setattr(session_module, "_assert_postgresql_schema_current",
                        lambda: calls.append("schema"))
    monkeypatch.setattr(session_module, "_seed_settings", lambda: calls.append("seed"))
    monkeypatch.setattr(session_module.Base.metadata, "create_all",
                        lambda *_args, **_kwargs: pytest.fail("must not create schema"))
    monkeypatch.setattr(session_module, "_add_missing_columns",
                        lambda: pytest.fail("must not patch schema"))

    session_module.init_db()

    assert calls == ["schema", "seed"]


def test_sqlite_init_keeps_create_patch_and_seed_path(monkeypatch) -> None:
    fake_engine = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    calls: list[str] = []
    monkeypatch.setattr(session_module, "engine", fake_engine)
    monkeypatch.setattr(session_module.Base.metadata, "create_all",
                        lambda *_args, **_kwargs: calls.append("create"))
    monkeypatch.setattr(session_module, "_add_missing_columns",
                        lambda: calls.append("patch"))
    monkeypatch.setattr(session_module, "_seed_settings", lambda: calls.append("seed"))

    session_module.init_db()

    assert calls == ["create", "patch", "seed"]
