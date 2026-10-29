"""separate automation results and finish operator/product migrations"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261029_2044"
down_revision: str | None = "d4f39fe694e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("image_jobs") as batch_op:
        batch_op.add_column(sa.Column("operator_id", sa.Integer(), nullable=True))

    with op.batch_alter_table("operators") as batch_op:
        batch_op.add_column(sa.Column(
            "role", sa.String(length=16), nullable=False, server_default="member"))

    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(sa.Column(
            "revision", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column(
            "automation_result", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_column("automation_result")
        batch_op.drop_column("revision")

    with op.batch_alter_table("operators") as batch_op:
        batch_op.drop_column("role")

    with op.batch_alter_table("image_jobs") as batch_op:
        batch_op.drop_column("operator_id")
