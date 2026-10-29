"""track which student owns each Miaoshou credential"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261030_0911"
down_revision: str | None = "20261030_0910"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(sa.Column("owner_operator_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("bound_by_operator_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("bound_at", sa.Float(), nullable=True))
        batch_op.create_foreign_key(
            "fk_accounts_owner_operator", "operators", ["owner_operator_id"], ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_accounts_bound_by_operator", "operators", ["bound_by_operator_id"], ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_constraint("fk_accounts_bound_by_operator", type_="foreignkey")
        batch_op.drop_constraint("fk_accounts_owner_operator", type_="foreignkey")
        batch_op.drop_column("bound_at")
        batch_op.drop_column("bound_by_operator_id")
        batch_op.drop_column("owner_operator_id")
