"""allow multiple software users to share one Miaoshou account."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261102_1001"
down_revision: str | None = "20261102_1000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
    rows = connection.execute(sa.text(
        "SELECT id, owner_operator_id, bound_by_operator_id, bound_at "
        "FROM accounts WHERE owner_operator_id IS NOT NULL"
    )).mappings()
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
    op.drop_index("ix_account_bindings_account", table_name="account_bindings")
    op.drop_table("account_bindings")
