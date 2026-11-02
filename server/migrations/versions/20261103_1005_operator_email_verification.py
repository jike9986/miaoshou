"""track whether each operator owns their recovery mailbox"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261103_1005"
down_revision: str | None = "20261102_1004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing mailbox values were supplied by an operator or an administrator;
    # none can be assumed to belong to the account holder.
    op.add_column(
        "operators",
        sa.Column("email_verified", sa.Boolean(), server_default=sa.false(),
                  nullable=False),
    )


def downgrade() -> None:
    op.drop_column("operators", "email_verified")
