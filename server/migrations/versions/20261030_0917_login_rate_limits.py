"""persisted rate limits for public software login"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261030_0917"
down_revision: str | None = "20261030_0916"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_rate_limits",
        sa.Column("bucket_key", sa.String(length=64), primary_key=True),
        sa.Column("window_started_at", sa.Float(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_login_rate_limits_locked_until",
        "login_rate_limits",
        ["locked_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_login_rate_limits_locked_until", table_name="login_rate_limits")
    op.drop_table("login_rate_limits")
