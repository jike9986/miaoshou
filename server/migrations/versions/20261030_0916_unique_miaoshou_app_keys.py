"""prevent one Miaoshou key from being bound to multiple accounts"""
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261030_0916"
down_revision: str | None = "20261030_0915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _assert_no_duplicate_app_keys(connection: sa.Connection) -> None:
    rows = connection.execute(sa.text(
        "SELECT id, app_key FROM accounts "
        "WHERE app_key IS NOT NULL AND app_key <> '' ORDER BY app_key, id"
    ))
    account_ids_by_key: dict[str, list[int]] = defaultdict(list)
    for account_id, app_key in rows:
        account_ids_by_key[str(app_key)].append(int(account_id))

    duplicates = [
        (app_key, account_ids)
        for app_key, account_ids in account_ids_by_key.items()
        if len(account_ids) > 1
    ]
    if not duplicates:
        return

    diagnostics = []
    for app_key, account_ids in duplicates:
        tail = app_key[-4:] if len(app_key) > 4 else "****"
        diagnostics.append(f"AppKey 尾号 {tail}：accounts.id={account_ids}")
    raise RuntimeError(
        "无法升级：accounts 中存在重复的非空妙手 AppKey，未修改数据。"
        "请人工核对并处理以下账号后重试；迁移不会自动合并或删除账号/凭据：\n"
        + "\n".join(diagnostics)
    )


def upgrade() -> None:
    connection = op.get_bind()
    _assert_no_duplicate_app_keys(connection)
    op.create_index(
        "uq_accounts_app_key_nonempty",
        "accounts",
        ["app_key"],
        unique=True,
        sqlite_where=sa.text("app_key <> ''"),
        postgresql_where=sa.text("app_key <> ''"),
    )


def downgrade() -> None:
    op.drop_index("uq_accounts_app_key_nonempty", table_name="accounts")
