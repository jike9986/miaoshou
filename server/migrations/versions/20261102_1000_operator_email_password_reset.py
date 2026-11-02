"""add operator recovery email and one-time password reset tokens"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261102_1000"
down_revision: str | None = "20261030_0917"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("operators", sa.Column("email", sa.String(length=254), nullable=True))
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operator_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=True),
        sa.Column("requested_ip", sa.String(length=64), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_hash"),
    )
    op.create_index(
        "ix_password_reset_tokens_operator",
        "password_reset_tokens",
        ["operator_id"],
    )
    op.create_index(
        "ix_password_reset_tokens_active",
        "password_reset_tokens",
        ["token_hash", "used_at", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_password_reset_tokens_active", table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_operator", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_column("operators", "email")
