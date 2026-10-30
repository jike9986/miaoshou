from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20261030_0916_unique_miaoshou_app_keys.py"
)
SPEC = importlib.util.spec_from_file_location("unique_miaoshou_app_keys", MIGRATION_PATH)
assert SPEC is not None and SPEC.loader is not None
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def _legacy_accounts(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, app_key VARCHAR(256))"
        ))


def _upgrade(connection: sa.Connection) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        MIGRATION.upgrade()


def test_duplicate_app_keys_block_migration_without_changing_rows_or_schema():
    engine = sa.create_engine("sqlite://")
    _legacy_accounts(engine)
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO accounts (id, app_key) VALUES "
            "(7, 'sensitive-app-key-1234'), (9, 'sensitive-app-key-1234')"
        ))

    with engine.begin() as connection:
        with pytest.raises(RuntimeError) as caught:
            _upgrade(connection)
        assert "accounts.id=[7, 9]" in str(caught.value)
        assert "1234" in str(caught.value)
        assert "sensitive-app-key" not in str(caught.value)
        assert connection.execute(sa.text("SELECT count(*) FROM accounts")).scalar_one() == 2
        indexes = sa.inspect(connection).get_indexes("accounts")
        assert "uq_accounts_app_key_nonempty" not in {row["name"] for row in indexes}

    engine.dispose()


def test_migration_adds_partial_unique_index_and_allows_empty_legacy_keys():
    engine = sa.create_engine("sqlite://")
    _legacy_accounts(engine)
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO accounts (id, app_key) VALUES (1, ''), (2, '')"
        ))
        _upgrade(connection)

    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO accounts (id, app_key) VALUES (3, 'key-a')"))
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text(
                "INSERT INTO accounts (id, app_key) VALUES (4, 'key-a')"
            ))

    engine.dispose()
