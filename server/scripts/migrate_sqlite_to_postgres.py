"""Preflight or copy a local SQLite database into an empty, migrated PostgreSQL DB.

The source is never modified. The destination is changed only with --apply, and
only when its schema is at this build's Alembic head and has no business rows.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Integer, MetaData, Table, create_engine, func, inspect, select, text
from sqlalchemy.engine import URL, Engine

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


def _source_engine(path: Path) -> Engine:
    return create_engine(URL.create("sqlite", database=str(path)))


def _read_source(
    engine: Engine, metadata: MetaData, decrypt_and_encrypt: Callable[[str], str]
) -> dict[str, list[dict[str, Any]]]:
    inspector = inspect(engine)
    actual = set(inspector.get_table_names()) - {"alembic_version"}
    expected = set(metadata.tables)
    if actual != expected:
        missing, extra = sorted(expected - actual), sorted(actual - expected)
        raise ValueError(f"source schema table mismatch; missing={missing}, extra={extra}")

    for table in metadata.sorted_tables:
        actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
        expected_columns = {column.name for column in table.columns}
        if actual_columns != expected_columns:
            raise ValueError(f"source schema columns do not match current model: {table.name}")

    with engine.connect() as connection:
        integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
        if integrity != "ok":
            raise ValueError("source SQLite integrity_check failed")
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchmany(1)
        if violations:
            raise ValueError("source SQLite contains foreign-key violations")

        result: dict[str, list[dict[str, Any]]] = {}
        for table in metadata.sorted_tables:
            rows = [dict(row) for row in connection.execute(select(table)).mappings()]
            if table.name == "accounts":
                for row in rows:
                    row["app_secret_enc"] = decrypt_and_encrypt(row["app_secret_enc"])
            result[table.name] = _order_self_references(table, rows)
    return result


def _order_self_references(table: Table, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    self_fks = [fk for fk in table.foreign_keys if fk.column.table is table]
    if not self_fks or len(rows) < 2:
        return rows

    primary_key = list(table.primary_key.columns)
    if len(primary_key) != 1:
        raise ValueError(f"cannot order self-referencing rows for {table.name}")
    pk_name = primary_key[0].name
    by_id = {row[pk_name]: row for row in rows}
    dependencies: dict[Any, set[Any]] = {key: set() for key in by_id}
    for fk in self_fks:
        local_name, remote_name = fk.parent.name, fk.column.name
        for row in rows:
            parent_id = row[local_name]
            if parent_id in by_id and parent_id != row[pk_name]:
                if remote_name != pk_name:
                    raise ValueError(f"unsupported self-reference for {table.name}")
                dependencies[row[pk_name]].add(parent_id)

    ordered: list[dict[str, Any]] = []
    ready = [key for key, required in dependencies.items() if not required]
    while ready:
        key = ready.pop()
        ordered.append(by_id[key])
        for candidate, required in dependencies.items():
            if key in required:
                required.remove(key)
                if not required and by_id[candidate] not in ordered and candidate not in ready:
                    ready.append(candidate)
    if len(ordered) != len(rows):
        raise ValueError(f"source contains a self-referencing cycle in {table.name}")
    return ordered


def _assert_empty_target(engine: Engine, metadata: MetaData) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing = set(metadata.tables) - tables
    extra = tables - set(metadata.tables) - {"alembic_version"}
    if missing:
        raise ValueError(f"target schema is incomplete; missing={sorted(missing)}")
    if extra:
        raise ValueError("target contains unrelated tables; refusing to touch that database")
    with engine.connect() as connection:
        _assert_rows_empty(connection, metadata)


def _assert_rows_empty(connection, metadata: MetaData) -> None:
    nonempty = [
        table.name
        for table in metadata.sorted_tables
        if connection.scalar(select(func.count()).select_from(table))
    ]
    if nonempty:
        raise ValueError(f"target is not empty; tables with rows={nonempty}")


def _set_sequences(connection, metadata: MetaData) -> None:
    if connection.dialect.name != "postgresql":
        return
    for table in metadata.sorted_tables:
        primary_key = list(table.primary_key.columns)
        if len(primary_key) != 1 or not isinstance(primary_key[0].type, Integer):
            continue
        column = primary_key[0]
        sequence = connection.scalar(
            text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
            {"table_name": table.name, "column_name": column.name},
        )
        if not sequence:
            continue
        maximum = connection.scalar(select(func.max(column)).select_from(table))
        if maximum is not None:
            connection.execute(
                text("SELECT setval(CAST(:sequence_name AS regclass), :value, true)"),
                {"sequence_name": sequence, "value": max(maximum, 1)},
            )


def _copy_rows(
    engine: Engine, metadata: MetaData, rows_by_table: dict[str, list[dict[str, Any]]]
) -> None:
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            for table in metadata.sorted_tables:
                quoted_name = table.name.replace('"', '""')
                connection.exec_driver_sql(f'LOCK TABLE "{quoted_name}" IN ACCESS EXCLUSIVE MODE')
        _assert_rows_empty(connection, metadata)
        for table in metadata.sorted_tables:
            rows = rows_by_table[table.name]
            for start in range(0, len(rows), 500):
                connection.execute(table.insert(), rows[start : start + 500])
        _set_sequences(connection, metadata)
        mismatched = []
        for table in metadata.sorted_tables:
            count = connection.scalar(select(func.count()).select_from(table))
            if count != len(rows_by_table[table.name]):
                mismatched.append(table.name)
        if mismatched:
            raise RuntimeError(f"post-import row count mismatch: {mismatched}")


def _backup_source(source: Path, backup_dir: Path, metadata: MetaData) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"{source.stem}-pre-postgres-{stamp}.db"
    if backup.exists():
        raise FileExistsError("backup path already exists")
    with sqlite3.connect(source) as connection:
        source_counts = {
            table.name: connection.execute(f'SELECT COUNT(*) FROM "{table.name}"').fetchone()[0]
            for table in metadata.sorted_tables
        }
        quoted_path = str(backup).replace("'", "''")
        connection.execute(f"VACUUM INTO '{quoted_path}'")
    with sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        violations = connection.execute("PRAGMA foreign_key_check").fetchmany(1)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        backup_counts = {
            table.name: connection.execute(f'SELECT COUNT(*) FROM "{table.name}"').fetchone()[0]
            for table in metadata.sorted_tables
        }
    if (
        integrity != "ok"
        or violations
        or tables != (set(metadata.tables) | (tables & {"alembic_version"}))
        or source_counts != backup_counts
    ):
        raise RuntimeError("source backup verification failed")
    return backup


def _print_manifest(rows_by_table: dict[str, list[dict[str, Any]]]) -> None:
    total = sum(len(rows) for rows in rows_by_table.values())
    print(f"Preflight passed: {len(rows_by_table)} tables, {total} rows.")
    for name, rows in rows_by_table.items():
        print(f"  {name}: {len(rows)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="existing local SQLite app.db")
    parser.add_argument(
        "--source-data-dir",
        type=Path,
        help="directory containing the source secret.key; defaults to source parent",
    )
    parser.add_argument(
        "--backup-dir", type=Path, help="backup destination; defaults to source parent/backups"
    )
    parser.add_argument("--apply", action="store_true", help="write to the empty PostgreSQL target")
    args = parser.parse_args(argv)

    source = args.source.resolve()
    if not source.is_file():
        parser.error("source SQLite file does not exist")
    source_data_dir = (args.source_data_dir or source.parent).resolve()
    if not os.environ.get("AOHUANG_DATABASE_URL", "").strip():
        parser.error("set AOHUANG_DATABASE_URL to the PostgreSQL target; the URL is never printed")

    runtime_data = tempfile.TemporaryDirectory(prefix="aohuang-pg-import-")
    os.environ["AOHUANG_DATA_DIR"] = runtime_data.name
    try:
        importlib.import_module("models")
        from db.session import Base, _assert_postgresql_schema_current, engine
        from services.crypto import (
            DPAPI_PREFIX,
            SERVER_PREFIX,
            decrypt,
            decrypt_with_local_secret,
            encrypt_for_server,
            validate_server_key,
        )

        if engine.dialect.name != "postgresql":
            raise ValueError("AOHUANG_DATABASE_URL must point to PostgreSQL")
        validate_server_key()
        _assert_postgresql_schema_current()
        _assert_empty_target(engine, Base.metadata)
        backup = None
        read_path = source
        if args.apply:
            backup_dir = (args.backup_dir or source.parent / "backups").resolve()
            backup = _backup_source(source, backup_dir, Base.metadata)
            read_path = backup

        source_engine = _source_engine(read_path)
        try:
            with source_engine.connect() as connection:
                encrypted = (
                    connection.exec_driver_sql(
                        "SELECT app_secret_enc FROM accounts WHERE app_secret_enc != ''"
                    )
                    .scalars()
                    .all()
                )
            needs_local_key = any(
                not value.startswith((DPAPI_PREFIX, SERVER_PREFIX)) for value in encrypted
            )
            source_key_path = source_data_dir / "secret.key"
            if needs_local_key and not source_key_path.is_file():
                raise ValueError("source secret.key is required for legacy Fernet credentials")
            source_secret = (
                source_key_path.read_text(encoding="utf-8").strip() if needs_local_key else ""
            )

            def reencrypt(encrypted_value: str) -> str:
                if encrypted_value.startswith((DPAPI_PREFIX, SERVER_PREFIX)):
                    plain = decrypt(encrypted_value)
                else:
                    plain = decrypt_with_local_secret(encrypted_value, source_secret)
                return encrypt_for_server(plain)

            rows = _read_source(
                source_engine,
                Base.metadata,
                reencrypt,
            )
        finally:
            source_engine.dispose()

        _print_manifest(rows)
        if not args.apply:
            print("Dry run only. No backup was written and the target was not changed.")
            return 0

        _copy_rows(engine, Base.metadata, rows)
        print(f"Import completed. Verified SQLite backup: {backup}")
        return 0
    except Exception as exc:  # noqa: BLE001 - SQLAlchemy errors may include bound values
        print(
            f"Migration aborted ({type(exc).__name__}); diagnostic details suppressed.",
            file=sys.stderr,
        )
        return 1
    finally:
        runtime_data.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
