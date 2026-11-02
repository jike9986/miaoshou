"""scope idempotency keys in publish logs to the software user"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261102_1004"
down_revision: str | None = "20261102_1003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("publish_logs", schema=None) as batch_op:
        batch_op.drop_constraint("uq_publish_logs_request", type_="unique")

    op.create_index(
        "uq_publish_logs_request_owner",
        "publish_logs",
        ["account_id", "detail_id", "shop_id", "operator_id"],
        unique=True,
        sqlite_where=sa.text("operator_id IS NOT NULL"),
        postgresql_where=sa.text("operator_id IS NOT NULL"),
    )
    op.create_index(
        "uq_publish_logs_request_unowned",
        "publish_logs",
        ["account_id", "detail_id", "shop_id"],
        unique=True,
        sqlite_where=sa.text("operator_id IS NULL"),
        postgresql_where=sa.text("operator_id IS NULL"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(sa.text(
        "SELECT account_id, detail_id, shop_id, COUNT(*) AS count "
        "FROM publish_logs GROUP BY account_id, detail_id, shop_id "
        "HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot restore account-wide publish-log uniqueness while multiple "
            "owners have records for the same detail and shop."
        )

    op.drop_index("uq_publish_logs_request_unowned", table_name="publish_logs")
    op.drop_index("uq_publish_logs_request_owner", table_name="publish_logs")
    with op.batch_alter_table("publish_logs", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_publish_logs_request", ["account_id", "detail_id", "shop_id"])
