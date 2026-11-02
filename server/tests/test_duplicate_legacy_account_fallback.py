from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import sessionmaker

from db import session as db_session
from db.session import Base
from models import Account, AccountBinding, Operator
from services import account_service

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20261103_1006_disable_duplicate_legacy_account_fallback.py"
)
SPEC = importlib.util.spec_from_file_location(
    "disable_duplicate_legacy_account_fallback_migration", MIGRATION_PATH
)
assert SPEC is not None and SPEC.loader is not None
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def test_alembic_migration_marks_duplicate_legacy_rows_without_deleting_data() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE accounts (id INTEGER PRIMARY KEY, owner_operator_id INTEGER, "
                "app_key TEXT NOT NULL, app_secret_enc TEXT NOT NULL)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE account_bindings (id INTEGER PRIMARY KEY, account_id INTEGER, "
                "operator_id INTEGER)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO accounts VALUES "
                "(1, 10, 'selected-key', 'selected-secret'), "
                "(2, 10, 'duplicate-key', 'duplicate-secret'), "
                "(3, 20, 'single-key', 'single-secret'), "
                "(4, 30, 'unbound-key', 'unbound-secret')"
            )
        )
        connection.execute(sa.text("INSERT INTO account_bindings VALUES (1, 1, 10), (2, 3, 20)"))

        context = MigrationContext.configure(connection)
        with Operations.context(context):
            MIGRATION.upgrade()

        rows = connection.execute(
            sa.text(
                "SELECT id, owner_operator_id, app_key, app_secret_enc, "
                "legacy_owner_fallback_disabled FROM accounts ORDER BY id"
            )
        ).all()

    assert rows == [
        (1, 10, "selected-key", "selected-secret", 0),
        (2, 10, "duplicate-key", "duplicate-secret", 1),
        (3, 20, "single-key", "single-secret", 0),
        (4, 30, "unbound-key", "unbound-secret", 0),
    ]
    engine.dispose()


def test_alembic_downgrade_refuses_to_remove_duplicate_owner_protection() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE accounts (id INTEGER PRIMARY KEY, owner_operator_id INTEGER, "
                "legacy_owner_fallback_disabled BOOLEAN NOT NULL DEFAULT FALSE)"
            )
        )
        connection.execute(sa.text(
            "INSERT INTO accounts (id, owner_operator_id, legacy_owner_fallback_disabled) "
            "VALUES (2, 10, TRUE)"
        ))

        context = MigrationContext.configure(connection)
        with Operations.context(context):
            try:
                MIGRATION.downgrade()
            except RuntimeError as exc:
                assert "duplicate legacy owner rows" in str(exc)
            else:
                raise AssertionError("downgrade must preserve duplicate-owner protection")

        columns = {row["name"] for row in sa.inspect(connection).get_columns("accounts")}
        assert "legacy_owner_fallback_disabled" in columns
        assert connection.execute(sa.text(
            "SELECT owner_operator_id, legacy_owner_fallback_disabled FROM accounts WHERE id=2"
        )).one() == (10, 1)
    engine.dispose()


def test_sqlite_startup_migration_keeps_duplicate_rows_blocked_after_unbind(monkeypatch) -> None:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_session, "engine", engine)
    with engine.begin() as connection:
        # Model an existing desktop database that predates the new marker.
        connection.exec_driver_sql(
            "ALTER TABLE accounts DROP COLUMN legacy_owner_fallback_disabled")
    db_session._add_missing_columns()
    make_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with make_session() as db:
        operator = Operator(
            name=f"legacy-owner-{uuid4().hex[:8]}",
            token=f"legacy-token-{uuid4().hex}",
            role="member",
        )
        db.add(operator)
        db.flush()
        owner_id = operator.id
        selected = Account(
            name="selected legacy account",
            app_key=f"key-{uuid4().hex}",
            app_secret_enc="encrypted-selected",
            owner_operator_id=owner_id,
        )
        duplicate = Account(
            name="duplicate legacy account",
            app_key=f"key-{uuid4().hex}",
            app_secret_enc="encrypted-duplicate",
            owner_operator_id=owner_id,
        )
        db.add_all([selected, duplicate])
        db.flush()
        selected_id, duplicate_id = selected.id, duplicate.id
        db.commit()

    db_session._backfill_sqlite_account_bindings()

    with make_session() as db:
        binding = db.query(AccountBinding).filter_by(operator_id=owner_id).one()
        assert binding.account_id == selected_id
        duplicate = db.get(Account, duplicate_id)
        assert duplicate is not None
        assert duplicate.legacy_owner_fallback_disabled is True
        assert duplicate.owner_operator_id == owner_id
        assert duplicate.app_secret_enc == "encrypted-duplicate"
        assert account_service.has_account_access(db, duplicate_id, owner_id) is False

        db.delete(binding)
        db.commit()

        assert account_service.has_account_access(db, duplicate_id, owner_id) is False
        assert db.query(Account).filter(Account.owner_operator_id == owner_id).count() == 2

    # Startup remains idempotent, including after a binding has been removed.
    db_session._backfill_sqlite_account_bindings()
    with make_session() as db:
        duplicate = db.get(Account, duplicate_id)
        assert duplicate is not None and duplicate.legacy_owner_fallback_disabled is True
        assert db.query(AccountBinding).filter_by(operator_id=owner_id).count() == 1
        assert account_service.has_account_access(db, duplicate_id, owner_id) is False

    engine.dispose()
