"""allow a student to bind and switch between multiple Miaoshou accounts"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261103_1009"
down_revision: str | None = "20261103_1008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("account_bindings", recreate="always") as batch:
            batch.drop_constraint("uq_account_binding_operator", type_="unique")
    else:
        op.drop_constraint("uq_account_binding_operator", "account_bindings",
                           type_="unique")

    # 1001 intentionally backfilled only one account per student. Materialize
    # every remaining legacy owner relation before the app stops using fallback.
    op.execute(sa.text(
        "INSERT INTO account_bindings "
        "(account_id, operator_id, bound_by_operator_id, bound_at) "
        "SELECT a.id, a.owner_operator_id, a.bound_by_operator_id, "
        "COALESCE(a.bound_at, 0.0) FROM accounts AS a "
        "WHERE a.owner_operator_id IS NOT NULL "
        "ON CONFLICT (account_id, operator_id) DO NOTHING"
    ))


def downgrade() -> None:
    connection = op.get_bind()
    duplicate_operator = connection.execute(sa.text(
        "SELECT operator_id FROM account_bindings "
        "GROUP BY operator_id HAVING COUNT(*) > 1 LIMIT 1"
    )).scalar_one_or_none()
    if duplicate_operator is not None:
        raise RuntimeError(
            "Cannot restore one-account-per-student constraint while multiple "
            f"bindings exist for operator_id={int(duplicate_operator)}."
        )
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("account_bindings", recreate="always") as batch:
            batch.create_unique_constraint("uq_account_binding_operator", ["operator_id"])
    else:
        op.create_unique_constraint("uq_account_binding_operator", "account_bindings",
                                    ["operator_id"])
