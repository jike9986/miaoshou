"""allow multiple software users to share one Miaoshou account."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261102_1001"
down_revision: str | None = "20261102_1000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNER_BACKFILL_QUERY = sa.text(
    "SELECT a.id, a.owner_operator_id, a.bound_by_operator_id, a.bound_at "
    "FROM accounts AS a "
    "WHERE a.owner_operator_id IS NOT NULL "
    "AND a.id = (SELECT MIN(a2.id) FROM accounts AS a2 "
    "           WHERE a2.owner_operator_id = a.owner_operator_id) "
    "ORDER BY a.owner_operator_id"
)


def upgrade() -> None:
    op.create_table(
        "account_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=False),
        sa.Column("bound_by_operator_id", sa.Integer(), nullable=True),
        sa.Column("bound_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["operator_id"], ["operators.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bound_by_operator_id"], ["operators.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("account_id", "operator_id", name="uq_account_binding_pair"),
        sa.UniqueConstraint("operator_id", name="uq_account_binding_operator"),
    )
    op.create_index("ix_account_bindings_account", "account_bindings", ["account_id"])

    # Preserve the legacy owner for existing users while making it an explicit
    # binding. Duplicate rows are ignored so upgrades remain repeatable.
    connection = op.get_bind()
    rows = connection.execute(_OWNER_BACKFILL_QUERY).mappings()
    for row in rows:
        connection.execute(sa.text(
            "INSERT INTO account_bindings "
            "(account_id, operator_id, bound_by_operator_id, bound_at) "
            "VALUES (:account_id, :operator_id, :bound_by, :bound_at) "
            "ON CONFLICT (operator_id) DO NOTHING"
        ), {
            "account_id": row["id"],
            "operator_id": row["owner_operator_id"],
            "bound_by": row["bound_by_operator_id"],
            "bound_at": row["bound_at"] or 0.0,
        })


def downgrade() -> None:
    connection = op.get_bind()
    unrepresentable = connection.execute(sa.text(
        "SELECT bindings.account_id, bindings.operator_id "
        "FROM account_bindings AS bindings "
        "LEFT JOIN accounts AS accounts ON accounts.id = bindings.account_id "
        "WHERE accounts.id IS NULL "
        "   OR accounts.owner_operator_id IS NULL "
        "   OR accounts.owner_operator_id <> bindings.operator_id "
        "ORDER BY bindings.account_id, bindings.operator_id LIMIT 1"
    )).first()
    if unrepresentable is not None:
        raise RuntimeError(
            "Cannot downgrade account bindings while relationships cannot be "
            "represented by accounts.owner_operator_id; "
            f"account_id={int(unrepresentable[0])}, "
            f"operator_id={int(unrepresentable[1])}."
        )
    op.drop_index("ix_account_bindings_account", table_name="account_bindings")
    op.drop_table("account_bindings")
