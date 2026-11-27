"""draft marks: remember text/image processing per Miaoshou draft

Revision ID: 20261127_0001
Revises: 20261119_0001
Create Date: 2026-11-27

一件妙手草稿 = 一行。`text_done` / `image_done` 分别记文字处理和图片处理
是否已经做过，处理过的下次直接跳过。

为什么必须是独立的表而不是给 products 加列：文字处理在**写回妙手草稿**
那一步就完成了，比入仓早。入仓失败时 products 里连一行都没有，
标记挂在 products 上必然跟着丢 —— 2026-11-27 实测 6 件商品正是如此
（草稿里的颜色已经是马来语，但 products 表查无此行）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261127_0001"
down_revision: str | None = "20261119_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "draft_marks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("detail_id", sa.String(length=32), nullable=False),
        sa.Column("shop_id", sa.String(length=32), nullable=False,
                  server_default=""),
        # 文字处理：写回草稿成功后置位，独立事务提交，不随读回核验回滚。
        sa.Column("text_done", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("text_at", sa.Float(), nullable=True),
        # 换语言必须重做，所以要记住上次用的语言。
        sa.Column("text_language", sa.String(length=16), nullable=False,
                  server_default=""),
        # 图片处理：全部任务 ready 且写回草稿后置位。
        sa.Column("image_done", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("image_at", sa.Float(), nullable=True),
        # 覆盖的待译格子数，数量变了说明商品换图，标记不能直接复用。
        sa.Column("image_total", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("created_at", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=True),
        sa.UniqueConstraint("account_id", "detail_id", "shop_id",
                            name="uq_draft_marks_identity"),
    )
    op.create_index("ix_draft_marks_lookup", "draft_marks",
                    ["account_id", "detail_id", "shop_id"])


def downgrade() -> None:
    op.drop_index("ix_draft_marks_lookup", table_name="draft_marks")
    op.drop_table("draft_marks")
