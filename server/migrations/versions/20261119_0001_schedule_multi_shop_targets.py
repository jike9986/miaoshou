"""schedules 支持一次任务发多个店铺（targets）"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261119_0001"
down_revision: str | None = "20261114_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 负责人 2026-11-19：定时任务也要能一次发多个店铺。
    # 用 JSON 存 [{shopId, template}]，不改 shop_id / template_name 两列 ——
    # 老任务照旧走单店铺路径，新任务把 targets 填满，两条路径共用同一个执行器。
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("targets", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.drop_column("targets")
