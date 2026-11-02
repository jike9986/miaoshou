"""prevent duplicate legacy account owners from regaining fallback access"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261103_1006"
down_revision: str | None = "20261103_1005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "legacy_owner_fallback_disabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    # Keep each legacy row and credential intact. Only prevent rows that are
    # not the operator's explicit account binding from using owner fallback.
    op.execute(
        sa.text(
            "UPDATE accounts "
            "SET legacy_owner_fallback_disabled = TRUE "
            "WHERE owner_operator_id IS NOT NULL "
            "AND EXISTS ("
            "  SELECT 1 FROM account_bindings AS binding "
            "  WHERE binding.operator_id = accounts.owner_operator_id "
            "  AND binding.account_id <> accounts.id"
            ")"
        )
    )


def downgrade() -> None:
    # Removing the marker would let pre-1006 account-access code fall back to
    # duplicate legacy owner_operator_id values once the explicit binding is
    # removed. Refuse this downgrade rather than silently re-open cross-user data.
    connection = op.get_bind()
    duplicate_owner = connection.execute(
        sa.text(
            "SELECT id FROM accounts "
            "WHERE legacy_owner_fallback_disabled IS TRUE LIMIT 1"
        )
    ).scalar_one_or_none()
    if duplicate_owner is not None:
        raise RuntimeError(
            "Cannot downgrade account fallback protection while duplicate legacy "
            "owner rows remain; resolve those rows before downgrading."
        )
    op.drop_column("accounts", "legacy_owner_fallback_disabled")
