"""keep scheduled tasks and their history private within shared accounts."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261102_1003"
down_revision: str | None = "20261102_1002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable is intentional: legacy scheduled rows have no reliable creator.
    # They remain visible to teachers/admins and are hidden from members until
    # explicitly recreated by an owner.
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_operator_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("schedule_logs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_operator_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_schedule_logs_owner_at",
                              ["owner_operator_id", "at"], unique=False)
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.create_index("ix_schedules_owner_created",
                              ["owner_operator_id", "created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("schedule_logs", schema=None) as batch_op:
        batch_op.drop_index("ix_schedule_logs_owner_at")
        batch_op.drop_column("owner_operator_id")
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.drop_index("ix_schedules_owner_created")
        batch_op.drop_column("owner_operator_id")
