"""add per-operator claims for shared warehouse products"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261030_0910"
down_revision: str | None = "20261029_2100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(),
                  sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_operator_id", sa.Integer(),
                  sa.ForeignKey("operators.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="claimed"),
        sa.Column("claimed_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("product_id", "owner_operator_id",
                            name="uq_product_claim_owner"),
    )
    op.create_index("ix_product_claim_owner_status", "product_claims",
                    ["owner_operator_id", "status"])
    # Legacy Product.claimed data has no reliable owner; preserve it without assignment.


def downgrade() -> None:
    op.drop_index("ix_product_claim_owner_status", table_name="product_claims")
    op.drop_table("product_claims")
