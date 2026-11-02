from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from db import session as db_session
from db.session import Base
from models import Account, Product
from services import product_service

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20261103_1007_scope_product_key_uniqueness.py"
)
SPEC = importlib.util.spec_from_file_location(
    "scope_product_key_uniqueness_migration", MIGRATION_PATH
)
assert SPEC is not None and SPEC.loader is not None
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def _account(db, name: str) -> Account:
    account = Account(
        name=name,
        app_key=f"key-{uuid4().hex}",
        app_secret_enc="encrypted-secret",
    )
    db.add(account)
    db.flush()
    return account


def test_same_miaoshou_product_identity_can_exist_in_different_accounts() -> None:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    make_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with make_session() as db:
        first = _account(db, "first")
        second = _account(db, "second")
        key = "same-detail:same-shop"
        db.add_all(
            [
                Product(
                    warehouse_key=key,
                    account_id=first.id,
                    detail_id="same-detail",
                    shop_id="same-shop",
                    title="first account copy",
                ),
                Product(
                    warehouse_key=key,
                    account_id=second.id,
                    detail_id="same-detail",
                    shop_id="same-shop",
                    title="second account copy",
                ),
            ]
        )
        db.commit()

        assert db.query(Product).filter_by(warehouse_key=key).count() == 2
        assert (
            db.query(Product)
            .filter_by(account_id=first.id, warehouse_key=key)
            .one()
            .title
            == "first account copy"
        )

        # The natural product identity remains unique within an account.
        db.add(
            Product(
                warehouse_key=key,
                account_id=first.id,
                detail_id="same-detail",
                shop_id="same-shop",
                title="duplicate in first account",
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("same-account product identity must remain unique")

    engine.dispose()


def test_upsert_product_never_updates_another_account_row() -> None:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    make_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with make_session() as db:
        first = _account(db, "first")
        second = _account(db, "second")
        key = "same-detail:same-shop"
        first_row = Product(
            warehouse_key=key,
            account_id=first.id,
            detail_id="same-detail",
            shop_id="same-shop",
            title="first account copy",
        )
        db.add(first_row)
        db.commit()

        updated = product_service.upsert_product(
            db,
            second.id,
            {
                "account_id": second.id,
                "detail_id": "same-detail",
                "shop_id": "same-shop",
                "title": "second account copy",
            },
            warehouse_key=key,
        )
        db.commit()

        assert updated.id != first_row.id
        assert db.get(Product, first_row.id).title == "first account copy"
        second_row = db.query(Product).filter_by(
            account_id=second.id, warehouse_key=key
        ).one()
        assert second_row.title == "second account copy"

    engine.dispose()


def test_key_only_resolution_reports_ambiguity_after_accounts_share_key() -> None:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    make_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with make_session() as db:
        first = _account(db, "first")
        second = _account(db, "second")
        db.add_all([
            Product(warehouse_key="ambiguous:key", account_id=first.id,
                    detail_id="ambiguous", shop_id="key"),
            Product(warehouse_key="ambiguous:key", account_id=second.id,
                    detail_id="ambiguous", shop_id="key"),
        ])
        db.commit()

        from core import errors
        from services import warehouse_service

        try:
            warehouse_service.resolve_product(db, "ambiguous:key")
        except errors.AppError as exc:
            assert exc.code == errors.AMBIGUOUS_PRODUCT
            assert exc.status == 409
        else:
            raise AssertionError("key-only lookup must report duplicate account scope")

    engine.dispose()


def _old_products_table(connection: sa.Connection) -> None:
    connection.exec_driver_sql(
        "CREATE TABLE products ("
        "id INTEGER PRIMARY KEY, warehouse_key VARCHAR(64) NOT NULL, "
        "account_id INTEGER, detail_id VARCHAR(32) NOT NULL, "
        "shop_id VARCHAR(32) NOT NULL, "
        "CONSTRAINT uq_products_identity UNIQUE (account_id, detail_id, shop_id), "
        "UNIQUE (warehouse_key))"
    )
    connection.exec_driver_sql(
        "INSERT INTO products (id, warehouse_key, account_id, detail_id, shop_id) "
        "VALUES (1, 'old-key', 11, 'detail', 'shop')"
    )


def _run_migration(connection: sa.Connection, operation: str) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(MIGRATION, operation)()


def test_sqlite_migration_drops_global_key_constraint_and_preserves_identity() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _old_products_table(connection)
        _run_migration(connection, "upgrade")

        constraints = sa.inspect(connection).get_unique_constraints("products")
        assert {tuple(item["column_names"]) for item in constraints} == {
            ("account_id", "detail_id", "shop_id")
        }
        connection.exec_driver_sql(
            "INSERT INTO products (id, warehouse_key, account_id, detail_id, shop_id) "
            "VALUES (2, 'old-key', 12, 'detail', 'shop')"
        )
        rows = connection.exec_driver_sql(
            "SELECT id, warehouse_key FROM products ORDER BY id"
        ).all()
        assert rows == [(1, "old-key"), (2, "old-key")]

    engine.dispose()


def test_downgrade_refuses_global_uniqueness_when_keys_are_shared() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _old_products_table(connection)
        _run_migration(connection, "upgrade")
        connection.exec_driver_sql(
            "INSERT INTO products (id, warehouse_key, account_id, detail_id, shop_id) "
            "VALUES (2, 'old-key', 12, 'detail', 'shop')"
        )

        try:
            _run_migration(connection, "downgrade")
        except RuntimeError as exc:
            assert "multiple accounts share a product key" in str(exc)
        else:
            raise AssertionError("downgrade must refuse to block cross-account rows")

        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM products WHERE warehouse_key='old-key'"
        ).scalar_one() == 2
        constraints = sa.inspect(connection).get_unique_constraints("products")
        assert {tuple(item["column_names"]) for item in constraints} == {
            ("account_id", "detail_id", "shop_id")
        }

    engine.dispose()


def test_sqlite_startup_rebuilds_legacy_product_key_constraint(monkeypatch) -> None:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(sa.text(
            'CREATE UNIQUE INDEX "legacy_products_warehouse_key" '
            'ON "products" ("warehouse_key")'
        ))
    monkeypatch.setattr(db_session, "engine", engine)

    # The helper is called after create_all/_add_missing_columns in real startup.
    db_session._ensure_sqlite_product_key_scope()
    inspector = sa.inspect(engine)
    assert not any(
        index.get("unique") and index.get("column_names") == ["warehouse_key"]
        for index in inspector.get_indexes("products")
    )
    # Insert through the ORM so client-side defaults fill the many non-null
    # product presentation fields in the current model.  The old database
    # schema used by this probe only needs to exercise the key constraint; raw
    # SQL with four columns would fail against the rebuilt current table.
    make_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with make_session() as db:
        first = _account(db, "first")
        second = _account(db, "second")
        db.add_all([
            Product(
                warehouse_key="same:key", account_id=first.id,
                detail_id="same", shop_id="key", title="first",
            ),
            Product(
                warehouse_key="same:key", account_id=second.id,
                detail_id="same", shop_id="key", title="second",
            ),
        ])
        db.commit()
        assert db.query(Product).filter_by(warehouse_key="same:key").count() == 2

    engine.dispose()
