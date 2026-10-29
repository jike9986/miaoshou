"""persist collect-box automation runs and per-item outcomes"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261030_0912"
down_revision: str | None = "20261030_0911"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_runs",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("owner_operator_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float()),
        sa.ForeignKeyConstraint(["owner_operator_id"], ["operators.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_automation_runs_owner_created", "automation_runs",
                    ["owner_operator_id", "created_at"])
    op.create_index("ix_automation_runs_account_shop", "automation_runs",
                    ["account_id", "shop_id", "created_at"])
    op.create_table(
        "automation_run_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("detail_id", sa.String(length=32), nullable=False),
        sa.Column("source_shop_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("process_status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("process_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("product_id", sa.Integer()),
        sa.Column("process_result", sa.JSON(), nullable=False),
        sa.Column("publish_status", sa.String(length=20), nullable=False,
                  server_default="not_requested"),
        sa.Column("publish_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("publish_result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["automation_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("run_id", "key", name="uq_automation_run_item_key"),
    )
    op.create_index("ix_automation_run_items_run_status", "automation_run_items",
                    ["run_id", "process_status"])


def downgrade() -> None:
    op.drop_index("ix_automation_run_items_run_status", table_name="automation_run_items")
    op.drop_table("automation_run_items")
    op.drop_index("ix_automation_runs_account_shop", table_name="automation_runs")
    op.drop_index("ix_automation_runs_owner_created", table_name="automation_runs")
    op.drop_table("automation_runs")
