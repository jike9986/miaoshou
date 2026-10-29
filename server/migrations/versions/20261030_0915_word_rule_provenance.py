"""record target site and rule references for violation words"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261030_0915"
down_revision: str | None = "20261030_0914"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("violation_words", sa.Column("site_scope", sa.String(8), nullable=True))
    op.add_column("violation_words", sa.Column("source_url", sa.String(512), nullable=True))
    op.add_column("violation_words", sa.Column("rule_version", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("violation_words", "rule_version")
    op.drop_column("violation_words", "source_url")
    op.drop_column("violation_words", "site_scope")
