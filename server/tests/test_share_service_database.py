from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.engine import make_url

from db.session import _create_engine
from services import share_service


class _Result:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value

    def fetchone(self) -> tuple[int]:
        return (self.value,)


class _Transaction:
    is_active = True

    def __init__(self) -> None:
        self.rolled_back = False

    def rollback(self) -> None:
        self.rolled_back = True
        self.is_active = False


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.transaction = _Transaction()
        self.closed = False

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return self.transaction

    def execute(self, statement: object, _params: object = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if "pg_database_size" in sql:
            return _Result(123456)
        if "lock_timeout" in sql:
            return _Result(750)
        return _Result(1)

    def close(self) -> None:
        self.closed = True


class _PostgresEngine:
    dialect = SimpleNamespace(name="postgresql")
    url = make_url(
        "postgresql+psycopg://private-user:private-password@db.example.test:5439/catalog"
    )

    def __init__(self) -> None:
        self.connections: list[_Connection] = []

    def connect(self) -> _Connection:
        connection = _Connection()
        self.connections.append(connection)
        return connection


def test_postgres_facts_use_safe_identity_database_size_and_rollback_probe(monkeypatch) -> None:
    engine = _PostgresEngine()
    monkeypatch.setattr(share_service, "engine", engine)
    share_service._write_probe_cache.clear()

    facts = share_service.db_facts(force_probe=True)

    assert facts == {
        "dbEngine": "postgresql",
        "dbPath": "PostgreSQL db.example.test:5439/catalog",
        "dbSizeBytes": 123456,
        "walEnabled": True,
        "dbWritable": True,
        "dbBusy": False,
        "dbError": "",
        "dbProbeCached": False,
    }
    assert all("private-user" not in value and "private-password" not in value
               for value in facts.values() if isinstance(value, str))
    statements = [sql for connection in engine.connections for sql in connection.statements]
    assert any("INSERT INTO settings" in sql and "CAST('null' AS JSON)" in sql
               for sql in statements)
    assert any("UPDATE settings SET updated_at" in sql for sql in statements)
    assert any("DELETE FROM settings" in sql for sql in statements)
    assert any(connection.transaction.rolled_back for connection in engine.connections)

    assert share_service.busy_timeout_ms() == 750
    assert engine.connections[-1].closed


def test_postgres_write_probe_error_does_not_expose_dsn(monkeypatch) -> None:
    class _BrokenEngine(_PostgresEngine):
        def connect(self) -> _Connection:
            raise RuntimeError(
                "postgresql://private-user:private-password@db.example.test/catalog failed"
            )

    monkeypatch.setattr(share_service, "engine", _BrokenEngine())
    share_service._write_probe_cache.clear()

    result = share_service.probe_writable(force=True)

    assert result["writable"] is False
    assert "private-password" not in result["error"]
    assert "db.example.test" not in result["error"]


def test_postgres_share_warnings_skip_sqlite_only_limits() -> None:
    warnings = share_service.share_warnings(
        False, "127.0.0.1", ["192.168.1.10"],
        {"dbEngine": "postgresql", "walEnabled": False,
         "dbSizeBytes": 900 * 1024 * 1024},
    )

    assert not any("WAL" in warning or "SQLite" in warning for warning in warnings)


def test_share_warnings_preserve_legacy_sqlite_defaults() -> None:
    warnings = share_service.share_warnings(
        False, "127.0.0.1", ["192.168.1.10"], {"walEnabled": False}
    )

    assert any("WAL" in warning for warning in warnings)


def test_sqlite_facts_keep_path_wal_probe_and_busy_timeout(monkeypatch, tmp_path) -> None:
    db_file = Path(tmp_path) / "app.db"
    engine = _create_engine(make_url(f"sqlite:///{db_file}"))
    monkeypatch.setattr(share_service, "engine", engine)
    monkeypatch.setattr(
        share_service, "get_settings", lambda: SimpleNamespace(db_path=db_file)
    )
    share_service._write_probe_cache.clear()
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
            assert share_service.busy_timeout_ms(connection) == 15_000

        facts = share_service.db_facts(force_probe=True)

        assert facts["dbEngine"] == "sqlite"
        assert facts["dbPath"] == str(db_file)
        assert facts["dbSizeBytes"] > 0
        assert facts["walEnabled"] is True
        assert facts["dbWritable"] is True
        assert facts["dbBusy"] is False
    finally:
        engine.dispose()
