"""bind upstream draft verification to a product revision"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261030_0914"
down_revision: str | None = "20261030_0913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("products", sa.Column("draft_verified_revision", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "draft_verified_revision")
