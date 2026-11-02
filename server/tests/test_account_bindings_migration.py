from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20261102_1001_account_bindings.py"
)
SPEC = importlib.util.spec_from_file_location("account_bindings_migration", MIGRATION_PATH)
assert SPEC is not None and SPEC.loader is not None
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def test_owner_backfill_query_compiles_for_postgresql() -> None:
    sql = str(MIGRATION._OWNER_BACKFILL_QUERY.compile(dialect=postgresql.dialect()))
    assert "SELECT a.id" in sql
    assert "MIN(a2.id)" in sql


def test_migration_backfills_lowest_account_id_per_operator() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE operators (id INTEGER PRIMARY KEY)"))
        connection.execute(sa.text(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, owner_operator_id INTEGER, "
            "bound_by_operator_id INTEGER, bound_at FLOAT)"
        ))
        connection.execute(sa.text("INSERT INTO operators (id) VALUES (10), (20)"))
        # Deliberately insert out of order to prove migration choice is explicit.
        connection.execute(sa.text(
            "INSERT INTO accounts (id, owner_operator_id, bound_by_operator_id, bound_at) "
            "VALUES (8, 10, 20, 8.0), (3, 10, 20, 3.0), (7, 20, NULL, NULL)"
        ))

        context = MigrationContext.configure(connection)
        with Operations.context(context):
            MIGRATION.upgrade()

        rows = connection.execute(sa.text(
            "SELECT account_id, operator_id, bound_by_operator_id, bound_at "
            "FROM account_bindings ORDER BY operator_id"
        )).all()
        assert rows == [(3, 10, 20, 3.0), (7, 20, None, 0.0)]

    engine.dispose()


def test_downgrade_refuses_bindings_not_representable_by_legacy_owner() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE operators (id INTEGER PRIMARY KEY)"
        ))
        connection.execute(sa.text(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, owner_operator_id INTEGER, "
            "bound_by_operator_id INTEGER, bound_at FLOAT)"
        ))
        connection.execute(sa.text(
            "INSERT INTO operators (id) VALUES (10), (20)"
        ))
        connection.execute(sa.text(
            "INSERT INTO accounts (id, owner_operator_id) VALUES (7, 10)"
        ))

        context = MigrationContext.configure(connection)
        with Operations.context(context):
            MIGRATION.upgrade()
        connection.execute(sa.text(
            "INSERT INTO account_bindings "
            "(id, account_id, operator_id, bound_at) VALUES (2, 7, 20, 2)"
        ))

        with Operations.context(context):
            try:
                MIGRATION.downgrade()
            except RuntimeError as exc:
                assert "cannot be represented" in str(exc)
                assert "account_id=7" in str(exc)
                assert "operator_id=20" in str(exc)
            else:
                raise AssertionError("downgrade must not discard shared-user bindings")

        assert connection.execute(sa.text(
            "SELECT COUNT(*) FROM account_bindings"
        )).scalar_one() == 2
    engine.dispose()
