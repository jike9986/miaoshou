from __future__ import annotations

import pytest
from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
)

from scripts.migrate_sqlite_to_postgres import (
    _assert_empty_target,
    _backup_source,
    _copy_rows,
    _order_self_references,
    _read_source,
)


def _metadata() -> MetaData:
    metadata = MetaData()
    Table(
        "nodes",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("nodes.id")),
        Column("name", String(32), nullable=False),
    )
    Table(
        "payloads",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("node_id", Integer, ForeignKey("nodes.id"), nullable=False),
        Column("body", JSON, nullable=False),
    )
    Table(
        "accounts",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("app_secret_enc", String, nullable=False),
    )
    return metadata


def test_self_referencing_rows_are_ordered_and_cycles_rejected() -> None:
    nodes = _metadata().tables["nodes"]
    rows = [
        {"id": 3, "parent_id": 2, "name": "child"},
        {"id": 2, "parent_id": 1, "name": "middle"},
        {"id": 1, "parent_id": None, "name": "root"},
    ]

    ordered = _order_self_references(nodes, rows)

    assert [row["id"] for row in ordered] == [1, 2, 3]
    with pytest.raises(ValueError, match="cycle"):
        _order_self_references(
            nodes,
            [
                {"id": 1, "parent_id": 2, "name": "one"},
                {"id": 2, "parent_id": 1, "name": "two"},
            ],
        )


def test_copy_preserves_ids_json_self_references_and_reencrypted_secret(tmp_path) -> None:
    metadata = _metadata()
    source = create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    metadata.create_all(source)
    metadata.create_all(target)
    with source.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.execute(
            metadata.tables["nodes"].insert(),
            [
                {"id": 10, "parent_id": None, "name": "root"},
                {"id": 11, "parent_id": 10, "name": "child"},
            ],
        )
        connection.execute(
            metadata.tables["payloads"].insert(),
            [
                {"id": 50, "node_id": 11, "body": {"labels": ["one", "two"]}},
            ],
        )
        connection.execute(
            metadata.tables["accounts"].insert(),
            [
                {"id": 7, "app_secret_enc": "source-envelope"},
            ],
        )

    rows = _read_source(source, metadata, lambda value: f"server:{value}")
    _copy_rows(target, metadata, rows)

    with target.connect() as connection:
        assert connection.execute(select(metadata.tables["nodes"].c.id)).scalars().all() == [10, 11]
        assert connection.execute(select(metadata.tables["payloads"].c.body)).scalar_one() == {
            "labels": ["one", "two"]
        }
        assert (
            connection.execute(select(metadata.tables["accounts"].c.app_secret_enc)).scalar_one()
            == "server:source-envelope"
        )
    source.dispose()
    target.dispose()


def test_preflight_rejects_nonempty_or_unrelated_target_tables(tmp_path) -> None:
    metadata = _metadata()
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    metadata.create_all(target)
    _assert_empty_target(target, metadata)
    with target.begin() as connection:
        connection.execute(insert(metadata.tables["nodes"]).values(id=1, name="occupied"))
    with pytest.raises(ValueError, match="not empty"):
        _assert_empty_target(target, metadata)
    target.dispose()

    unrelated = create_engine(f"sqlite:///{tmp_path / 'unrelated.db'}")
    metadata.create_all(unrelated)
    with unrelated.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE other_app_data (id INTEGER PRIMARY KEY)")
    with pytest.raises(ValueError, match="unrelated tables"):
        _assert_empty_target(unrelated, metadata)
    unrelated.dispose()


def test_backup_is_verified_and_source_remains_unchanged(tmp_path) -> None:
    metadata = _metadata()
    source_path = tmp_path / "source.db"
    source = create_engine(f"sqlite:///{source_path}")
    metadata.create_all(source)
    with source.begin() as connection:
        connection.execute(metadata.tables["nodes"].insert().values(id=1, name="root"))

    backup = _backup_source(source_path, tmp_path / "backups", metadata)

    assert backup.is_file()
    second_backup = _backup_source(source_path, backup.parent, metadata)
    assert second_backup != backup
    with source.connect() as connection:
        assert connection.execute(select(metadata.tables["nodes"].c.id)).scalars().all() == [1]
    source.dispose()
