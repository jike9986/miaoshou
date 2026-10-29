"""add operator login credentials"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261029_2100"
down_revision: str | None = "20261029_2044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("operators") as batch_op:
        batch_op.add_column(sa.Column("username", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("password_hash", sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column(
            "must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.create_unique_constraint("uq_operators_username", ["username"])


def downgrade() -> None:
    with op.batch_alter_table("operators") as batch_op:
        batch_op.drop_constraint("uq_operators_username", type_="unique")
        batch_op.drop_column("must_change_password")
        batch_op.drop_column("password_hash")
        batch_op.drop_column("username")
