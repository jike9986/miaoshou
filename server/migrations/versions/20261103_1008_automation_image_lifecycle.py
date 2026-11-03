"""persist automation image completion separately from the publish payload"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261103_1008"
down_revision: str | None = "20261103_1007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("products", sa.Column("automation_images", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "automation_images")
