from __future__ import annotations

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


def test_application_startup_blocks_unfinished_postgres_runtime(monkeypatch) -> None:
    engine = _create_engine(
        make_url("postgresql+psycopg://user:password@localhost:5432/catalog")
    )
    monkeypatch.setattr(session_module, "engine", engine)
    try:
        with pytest.raises(RuntimeError, match="startup is not supported yet"):
            session_module.init_db()
    finally:
        engine.dispose()
