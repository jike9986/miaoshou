"""add email OTP challenges and normalized mailbox uniqueness"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261102_1002"
down_revision: str | None = "20261102_1001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _assert_no_duplicate_emails()
    op.create_table(
        "email_verification_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index(
        "ix_email_codes_lookup", "email_verification_codes",
        ["purpose", "email", "expires_at"],
    )
    op.create_index(
        "ix_email_codes_expires_at", "email_verification_codes", ["expires_at"]
    )
    op.create_index(
        "uq_email_codes_active", "email_verification_codes",
        ["purpose", "email"], unique=True,
        sqlite_where=sa.text("used_at IS NULL"),
        postgresql_where=sa.text("used_at IS NULL"),
    )
    op.create_index(
        "uq_operators_email_normalized", "operators",
        [sa.text("lower(trim(email))")], unique=True,
        sqlite_where=sa.text("email IS NOT NULL AND trim(email) <> ''"),
        postgresql_where=sa.text("email IS NOT NULL AND trim(email) <> ''"),
    )


def downgrade() -> None:
    op.drop_index("uq_operators_email_normalized", table_name="operators")
    op.drop_index("uq_email_codes_active", table_name="email_verification_codes")
    op.drop_index("ix_email_codes_expires_at", table_name="email_verification_codes")
    op.drop_index("ix_email_codes_lookup", table_name="email_verification_codes")
    op.drop_table("email_verification_codes")


def _assert_no_duplicate_emails() -> None:
    connection = op.get_bind()
    duplicate_emails = connection.execute(sa.text(
        "SELECT lower(trim(email)) AS normalized_email FROM operators "
        "WHERE email IS NOT NULL AND trim(email) <> '' "
        "GROUP BY lower(trim(email)) HAVING COUNT(*) > 1 "
        "ORDER BY MIN(id)"
    )).scalars().all()
    if duplicate_emails:
        groups = []
        for email in duplicate_emails:
            ids = connection.execute(sa.text(
                "SELECT id FROM operators WHERE lower(trim(email)) = :email ORDER BY id"
            ), {"email": email}).scalars().all()
            groups.append(
                f"operators.id=[{','.join(str(int(value)) for value in ids)}]")
        raise RuntimeError(
            "Cannot add normalized email uniqueness because duplicate mailbox rows exist. "
            "No email values were changed; review these operator IDs first: "
            + "; ".join(groups)
        )
