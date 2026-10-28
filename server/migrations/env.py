"""Alembic 运行环境。

三条关键设置，少一条后面一定踩：

  · `target_metadata = Base.metadata`
    必须是 `models/__init__.py` 汇总的那个 Base —— 该文件把 13 个模型类全部
    import 了一遍，导入即注册到 metadata。少导一个模型，autogenerate 就会
    认为那张表「应该被删除」，生成一条 drop_table 出来。

  · `render_as_batch=True`
    SQLite 不支持 ALTER COLUMN / DROP COLUMN（老版本）等语法，改列必须走
    「建新表 → 拷数据 → 换名」的 batch 模式。不开这个开关，将来第一次
    「给已有列改类型」的迁移会直接报 OperationalError。

  · `compare_type=True` / `compare_server_default=True`
    默认这两项是关的，autogenerate 只比对「列有没有增减」，看不到
    String(32) → String(64)、默认值从 '' 变成 'active' 这类变化。

连接串不写死在 alembic.ini 里，而是从 core.config.get_settings().db_path 取，
这样 `AOHUANG_DATA_DIR` 指哪、迁移就改哪（CI 与本地试验都靠这一点把迁移
关进临时目录，绝不碰用户真实库）。
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# server/ 加入 sys.path：允许从仓库根执行 `python -m alembic -c server/alembic.ini ...`
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 导入 models 包即把全部模型注册进 Base.metadata（顺序敏感，必须在读 metadata 前）。
# 这里必须在 fileConfig 之后 import：项目自己的 logger 配置要先就位。
import models  # noqa: E402,F401  —— 副作用导入，不能删
from core.config import get_settings  # noqa: E402
from db.session import Base, engine  # noqa: E402

settings = get_settings()
# sqlite:/// 后面跟的是路径；Windows 反斜杠在 URL 里要统一成正斜杠
DB_URL = f"sqlite:///{settings.db_path.as_posix()}"
config.set_main_option("sqlalchemy.url", DB_URL)

target_metadata = Base.metadata


def _include_object(object_, name, type_, reflected, compare_to):  # noqa: ANN001, ARG001
    """过滤不该进迁移的对象。

    `alembic_version` 由 Alembic 自己维护，出现在 autogenerate 的 diff 里
    只会制造噪音（尤其是首次生成基线时）。
    """
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    """离线模式：只把 SQL 打到 stdout，不连库（`alembic upgrade head --sql`）。"""
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # 离线出 SQL 时同样需要 batch 语义，否则 SQLite 的 ALTER 语句出不来
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：复用应用自己的 engine。

    复用而不是新建 engine 的理由：`db/session.py` 在 engine 上挂了 connect
    事件，会打开 `foreign_keys=ON`。迁移里如果有涉及外键的批次重建，用别的
    engine 会静默跳过外键校验，把脏数据写进去。
    """
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite 改列必须 batch；这条是硬要求，见文件头说明
            render_as_batch=True,
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
            # 列注释/表注释在 SQLite 上没有意义，关掉避免生成无法执行的迁移
            include_schemas=False,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
