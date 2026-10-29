"""store verified product measurements separately from publish payload"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261030_0913"
down_revision: str | None = "20261030_0912"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("products", sa.Column("quality_review", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "quality_review")
