"""store selected products and planned run count on scheduled publications"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261103_1010"
down_revision: str | None = "20261103_1009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("selected_product_ids", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("planned_runs", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("completed_runs", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.drop_column("planned_runs")
        batch_op.drop_column("completed")
        batch_op.drop_column("completed_runs")
        batch_op.drop_column("selected_product_ids")
