# 迁移目录

这个目录由 Alembic 管理。**不要手改 `versions/` 里已提交的迁移文件** ——
别人本地库的 `alembic_version` 记着那个 revision，改了内容不会重放，
只会让「新库」和「老库」结构悄悄分叉。要改就再写一条新迁移。

## 常用命令

在 `server/` 目录下执行。推荐走 `scripts/db.py`，它默认把目标库钉在临时目录，
不会误改用户的真实 `app.db`：

| 目的 | 命令 |
| --- | --- |
| 建库到最新（临时库） | `python scripts/db.py upgrade` |
| 改完 model 后生成迁移 | `python scripts/db.py revision -m "给 products 加 xxx 列"` |
| 回退一步 | `python scripts/db.py downgrade -1` |
| 看迁移链 | `python scripts/db.py history` |
| 全链路自检 | `python scripts/db.py check` |
| 直接调 alembic | `python -m alembic upgrade head` |

直接调 `alembic` 时必须先自己设 `AOHUANG_DATA_DIR`，否则会连到默认数据目录
（`%LOCALAPPDATA%\aohuang-desktop`）—— 那是用户真实库。

## 三个目录文件

- `env.py` —— 运行环境。关键设置：`target_metadata` 指向 `db.session.Base.metadata`，
  `render_as_batch=True`（SQLite 改列必须走 batch），`compare_type=True` +
  `compare_server_default=True`（否则看不到类型/默认值变化）。
  连接串不写死在 `alembic.ini` 里，而是复用应用 database engine 的 URL
  （由 `AOHUANG_DATABASE_URL` 或默认本机 SQLite 配置决定）。
- `script.py.mako` —— 新迁移的模板。
- `versions/` —— 迁移脚本。当前迁移头为 `20261030_0912`。

## 和 `_add_missing_columns()` 的分工

`db/session.py` 里有一个 `_add_missing_columns()`，它用 `ALTER TABLE ADD COLUMN`
给老库补列。它和 Alembic **不是二选一，各管一段**：

| 场景 | 谁负责 | 原因 |
| --- | --- | --- |
| 新库从零建表 | **Alembic** | 有版本记录，能重放，能校验 |
| 老用户库升级补新列 | **`_add_missing_columns()`** | 用户双击 exe 升级，不会手动跑 alembic |
| 改列类型 / 删列 / 改约束 | **只能 Alembic** | `ADD COLUMN` 做不到 |
| SQLite 应用启动 | **`create_all` + 补列** | 兼容桌面端既有 SQLite 库 |
| PostgreSQL 应用启动 | **校验连接与迁移 heads** | 不自动升级 schema；head 不一致则拒绝启动 |

因此：**新加一列时，模型定义和 Alembic 迁移都要有**。只改模型不写迁移，
新库（走 alembic 建的）会缺这列；只写迁移不改模型，下次 autogenerate 会把
这条迁移「撤销」掉。

SQLite 启动路径刻意**不接** `alembic upgrade head`：打包后的 sidecar 里没有 `migrations/`
目录，而且老用户库的表已经存在，upgrade 会因 `table already exists` 失败。
PostgreSQL 启动会检查连接和 revision heads，但不自动执行迁移。PyInstaller 当前也未打包
迁移目录，因此源码环境的 PostgreSQL 配置不代表打包版已可用；打包部署路径仍需实现并验收。

## 老库如何纳入版本管理（一次性）

已经用 `create_all` 建好的库（有表、没有 `alembic_version`）不能直接
`upgrade head`，会报表已存在。正确做法是「打戳」：

```
# 先把真实库备份一份，再执行
python scripts/db.py stamp head --real
```

`stamp` 只写 `alembic_version` 一行，不动任何业务表。之后这个库就和迁移链
对齐了，后续新迁移可以正常 upgrade。

`scripts/db.py check` 的 [4] 号用例完整验证了这条路径（create_all 建库 →
直接 upgrade 失败 → stamp head → 再 upgrade 成功）。

## 迁移清单

| revision | 内容 |
| --- | --- |
| `f6add52fca2e` | 基线：13 张表 |
| `e3079cb089af` | 新增 `operators`、`audit_logs`（操作员与审计） |
| `d4f39fe694e0` | `batches`/`products`/`publish_logs` 加 `operator_id`（可空，batch） |
| `20261029_2044` | 拆分自动化结果商品并完成操作员/商品迁移 |
| `20261029_2100` | 增加操作员登录凭据 |
| `20261030_0910` | 增加按操作员隔离的成品认领 |
| `20261030_0911` | 增加妙手凭据的学员归属 |
| `20261030_0912` | 持久化采集箱自动化任务和逐商品结果 |

基线的生成方式：把 `AOHUANG_DATA_DIR` 指到 `tempfile.mkdtemp()`，对空库跑
`alembic revision --autogenerate`，因此里面全是 `create_table`。

整条链已核对与 `Base.metadata.create_all()` 等价：

| 项 | create_all | 迁移链 |
| --- | --- | --- |
| 表 | 18 | 18 |
| SQLite 索引（含自动索引） | 51 | 51 |
| 外键 | 20 | 20 |

复核命令：`python migrations/_verify_baseline.py`（逐列比对类型 / 非空 /
默认值 / 主键，逐表比对索引与外键，两份库都建在临时目录）。
CI 迁移检查另行统计出 33 个显式索引和 18 个 SQLite 自动唯一/主键索引。

当前 18 张表：
`accounts` `audit_logs` `automation_run_items` `automation_runs` `batch_items` `batches`
`categories` `groups` `image_jobs` `operators` `product_claims` `products` `publish_logs`
`schedule_logs` `schedules` `settings` `shops` `violation_words`。

PostgreSQL 有连接配置、迁移执行路径和启动 revision 校验。受控 SQLite 导入工具已实现并有 SQLite
算法测试，但尚无真实 PostgreSQL CRUD/在线迁移/导入验收，也没有 PostgreSQL 备份恢复演练。

期望值不要硬编码 —— `scripts/ci_migration_check.py` 与
`migrations/_selftest.py` 都从 `Base.metadata` 派生表集合，模型加了表会自动跟上。

## 加一列的完整流程

1. 改模型（`models/*.py`），例如在 `Product` 上加
   `memo: Mapped[str] = mapped_column(Text, default="", server_default="")`
2. 生成迁移：`python scripts/db.py revision -m "products 加 memo 列"`
3. 打开生成的迁移，确认 `upgrade()` 里有 `add_column`、`downgrade()` 里有
   `drop_column`（在 `batch_alter_table` 块里）
4. 本地验证：
   - `python scripts/db.py check`（全链路自检）
   - `python migrations/_verify_baseline.py`（确认模型与迁移仍等价）
5. 提交迁移文件，与模型改动放同一个 commit

`server/scripts/db.py` 会在 autogenerate 产出空迁移时自动删除它并提示，
避免历史里堆一串什么都没做的版本号。

## CI 里跑什么

`.github/workflows/ci.yml` 的 `python` job 会在临时目录里跑
`alembic upgrade head` → 列出建出的表 → `alembic downgrade base` →
断言业务表清空。**CI 从不碰真实 `app.db`**。
