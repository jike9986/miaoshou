"""add multi_device flag and operator_tokens table"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261114_0001"
down_revision: str | None = "20261103_1010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operator_id", sa.Integer(),
                  sa.ForeignKey("operators.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(128), unique=True, nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_operator_tokens_operator_id", "operator_tokens", ["operator_id"])
    op.create_index("ix_operator_tokens_token_hash", "operator_tokens", ["token_hash"])

    # 用 alembic 标准 API，同时兼容 SQLite（CI）和 PostgreSQL（生产）
    with op.batch_alter_table("operators", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("multi_device", sa.Boolean(), nullable=False,
                      server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("operators", schema=None) as batch_op:
        batch_op.drop_column("multi_device")

    op.drop_index("ix_operator_tokens_token_hash", table_name="operator_tokens")
    op.drop_index("ix_operator_tokens_operator_id", table_name="operator_tokens")
    op.drop_table("operator_tokens")
