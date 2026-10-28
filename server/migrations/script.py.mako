"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    # 改已有列（类型 / 可空性 / 默认值）时必须用 batch 模式：
    #     with op.batch_alter_table("表名") as batch_op:
    #         batch_op.alter_column(...)
    # SQLite 不支持直接 ALTER COLUMN，batch 会走「建新表→拷数据→改名」。
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    # downgrade 必须真的能跑通（CI 会执行 `alembic downgrade base` 验一遍）。
    # 写不出来就说明这次迁移的方向本身有问题，别用 pass 糊过去。
    ${downgrades if downgrades else "pass"}
