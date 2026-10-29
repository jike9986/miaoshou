# 数据库迁移

本项目用 **Alembic** 管理表结构，同时保留一个运行时的补列函数
`_add_missing_columns()`。两者不是二选一，各自负责一段，混着用才对。

## 一句话结论

| 场景 | 谁负责 |
| --- | --- |
| 新库从零建表 | **Alembic** |
| SQLite 应用启动 | `create_all` + `_add_missing_columns()` |
| PostgreSQL 应用启动 | 检查连接与迁移 heads；不自动执行迁移 |
| 老用户库升级补新列 | `_add_missing_columns()` |
| 改列类型 / 删列 / 改名 / 改约束 | **只能 Alembic** |
| 开发时改完模型生成迁移 | `python scripts/db.py revision -m "..."` |

## 为什么不把 Alembic 挂到启动流程上

早期设计（`docs/architecture.md` §3.13 的初版）设想「启动时
`alembic upgrade head`，失败就让 sidecar 退出」。落地时这条路有两个硬阻塞：

1. **打包产物里没有迁移目录**。`server.spec` 用 PyInstaller onedir 打包，
   `migrations/` 不在 `datas` 里，运行时 `script_location` 指向的目录不存在。
2. **老用户库的表已经存在**。他们的 `app.db` 是 `create_all` 建的，没有
   `alembic_version` 表。直接 `upgrade head` 会执行基线迁移的
   `op.create_table('accounts')`，报 `table accounts already exists`，
   应用起不来 —— 相当于一次升级把所有老用户挡在门外。

实测证据：`python scripts/db.py check` 的第 [4b] 步就是这个场景，
输出 `exit=1（预期非 0：表已存在）`。

所以 SQLite 启动路径保持 `create_all` + `_add_missing_columns()`，Alembic 负责新库建表和结构变更。
PostgreSQL 启动要求数据库已由受控迁移流程升级到当前 heads，不会自动修改 schema。
当前 PyInstaller 配置没有把 `migrations/` 打进 sidecar，而 PostgreSQL 启动检查需要读取迁移脚本；
因此打包版 PostgreSQL 部署尚未验证，也不能仅通过配置连接串启用。

## 常用命令

在 `server/` 目录下执行。推荐走 `scripts/db.py`：

| 目的 | 命令 |
| --- | --- |
| 建到最新（临时库） | `python scripts/db.py upgrade` |
| 生成迁移 | `python scripts/db.py revision -m "给 products 加 memo 列"` |
| 回退一步 | `python scripts/db.py downgrade -1` |
| 看迁移链 | `python scripts/db.py history` |
| 全链路自检 | `python scripts/db.py check` |
| 直接调 alembic | `python -m alembic upgrade head` |

`scripts/db.py` 的默认行为：把 `AOHUANG_DATA_DIR` 指到一个
`tempfile.mkdtemp()` 目录，命令跑完删掉。**默认不会碰真实库。**
要操作真实库必须显式加 `--real`，脚本会打印目标路径并要求输入 `yes`。

直接 `python -m alembic ...` 时不会自动设环境变量，会连到默认数据目录
（`%LOCALAPPDATA%\aohuang-desktop`）—— 那是用户真实库，注意别在生产机上乱跑。

## 目录与文件

```
server/
├─ alembic.ini              配置（连接串刻意留空，由 env.py 注入）
├─ migrations/
│  ├─ env.py                运行环境（target_metadata / batch / compare_*）
│  ├─ script.py.mako        新迁移的模板
│  ├─ _selftest.py          迁移链自检（scripts/db.py check 调的）
│  ├─ _verify_baseline.py   模型 vs 基线迁移的逐列等价性核对
│  ├─ README.md             本目录速查
│  └─ versions/
│     └─ 20261028_2020_f6add52fca2e_baseline_existing_13_tables.py
└─ scripts/
   └─ db.py                 便捷入口（默认临时库）
```

### `env.py` 的三条关键设置

| 设置 | 为什么必须有 |
| --- | --- |
| `target_metadata = Base.metadata` | 来自 `db/session.py`。`models/__init__.py` 把全部模型 import 了一遍，导入即注册。少导一个模型，autogenerate 就会认为那张表该被删掉 |
| `render_as_batch=True` | SQLite 不支持 `ALTER COLUMN` / `DROP COLUMN`。改列必须走「建新表→拷数据→改名」的 batch 模式，不开这个开关第一次改列就 `OperationalError` |
| `compare_type=True` + `compare_server_default=True` | 默认是关的，只比对列有没有增减。看不到 `String(32)→String(64)`、默认值从 `''` 变成 `'active'` 这类变化 |

连接串不写死在 `alembic.ini` 里，而是：

```python
DB_URL = engine.url
```

写死的后果是「应用写 A 库、迁移改 B 库」，而且输出看不出来：迁移报成功，
应用照样缺列。

### `alembic.ini` 为什么全是英文

Alembic 用 `encoding="locale"` 读 ini（`alembic/util/compat.py`）。这台机器
locale 是 GBK，ini 里写中文注释会在启动前就抛 `UnicodeDecodeError`。
中文说明都放在 `docs/migrations.md` 和 `migrations/README.md` 里。

## 迁移清单

| revision | 内容 |
| --- | --- |
| `f6add52fca2e` | 基线：13 张表 |
| `e3079cb089af` | 新增 `operators`、`audit_logs`（操作员与审计） |
| `d4f39fe694e0` | 给 `batches` / `products` / `publish_logs` 加 `operator_id`（可空，batch 模式） |
| `20261029_2044` | 拆分自动化结果商品并完成操作员/商品迁移 |
| `20261029_2100` | 增加操作员登录凭据 |
| `20261030_0910` | 增加按操作员隔离的成品认领 |
| `20261030_0911` | 增加妙手凭据的学员归属 |
| `20261030_0912` | 持久化采集箱自动化任务和逐商品结果 |

### 基线（`20261028_2020_f6add52fca2e_baseline_existing_13_tables.py`）

生成方式：`AOHUANG_DATA_DIR` 指向 `tempfile.mkdtemp()`，对空库跑
`alembic revision --autogenerate`，所以里面全是 `create_table`（没有一条
`alter`，也没有 `drop_table`）。

### 第二条（`20261028_2034_e3079cb089af_add_audit_logs_and_operators.py`）

由 `python scripts/db.py revision -m "add audit_logs and operators"` 生成。
这条是**新增表**，不改已有表，所以老库直接 `upgrade` 即可，不需要 batch。

### 第三条（`20261028_2037_d4f39fe694e0_add_operator_id_to_batches_products_.py`）

给三张已有表加可空列。因为改的是已有表，**必须走 batch 模式**：

```python
with op.batch_alter_table("batches") as batch_op:
    batch_op.add_column(sa.Column("operator_id", sa.Integer(), nullable=True))
```

列是可空的（`nullable=True`）且没有 `server_default`，所以老库补列时
已有行直接填 NULL，`ALTER TABLE ADD COLUMN` 能正常执行。
**如果这列是非空且无默认值，SQLite 会拒绝执行** —— 加列时务必注意。

### 当前迁移链检查

CI 在 SQLite 临时库验证完整迁移链的 upgrade/downgrade：

| 项 | create_all | 迁移链 |
| --- | --- | --- |
| 表 | 18 | 18 |
| SQLite 索引（含自动索引） | 51 | 51 |
| 外键 | 20 | 20 |

CI 的迁移检查另行统计出 33 个显式索引和 18 个 SQLite 自动唯一/主键索引。

复核命令：

```bash
cd server && python migrations/_verify_baseline.py
```

输出：

```
表数量：create_all=18 / 迁移=18
索引数量：create_all=51 / 迁移=51
外键数量：create_all=20 / 迁移=20
[结论] 当前 SQLite 临时库模型与迁移链等价；不代表 PostgreSQL 在线验收通过
```

它比对的是逐列的「类型 + 是否非空 + 默认值 + 是否主键」，以及逐表的索引
（含 UNIQUE 约束自动建的索引）与外键（含 `ondelete` 行为）。
两份库分别建在各自的临时目录、各自的子进程里 —— `db/session.py` 的 engine
是模块级单例，同进程换环境变量不会生效。

当前 18 张表：
`accounts` `audit_logs` `automation_run_items` `automation_runs` `batch_items` `batches`
`categories` `groups` `image_jobs` `operators` `product_claims` `products` `publish_logs`
`schedule_logs` `schedules` `settings` `shops` `violation_words`。

PostgreSQL 可配置连接并在启动时检查 schema revision。受控 SQLite 导入工具已实现，但尚无真实
PostgreSQL 服务上的 CRUD、迁移或导入验收；PostgreSQL 备份/恢复演练也尚未实现。不要仅通过设置
连接串就迁移生产数据或启动共享服务。

### SQLite 到 PostgreSQL 的受控导入

`server/scripts/migrate_sqlite_to_postgres.py` 默认只做预检。它要求源库表/列与当前模型一致，
检查 SQLite integrity 与外键，并验证每个非空妙手密文可解密；目标必须是当前 Alembic head、
仅含本应用表且没有业务数据的 PostgreSQL 库。存在其它表、目标有数据或源结构不匹配都会中止。

先通过受保护的环境配置提供 `AOHUANG_DATABASE_URL` 和独立的 `AOHUANG_CREDENTIAL_KEY`，
再从 `server/` 目录运行：

```bash
python scripts/migrate_sqlite_to_postgres.py --source "D:/aohuang-data/app.db"
python scripts/migrate_sqlite_to_postgres.py --source "D:/aohuang-data/app.db" --apply
```

不带 `--apply` 不写备份、不改目标。正式导入会先用 `VACUUM INTO` 生成本地 SQLite 快照并校验，
再从该快照复制数据，在一个 PostgreSQL 事务中保留主键、重加密妙手凭据、对齐自增序列并核对各表行数。
默认备份位置是源库旁的 `backups/`，不会自动清理旧备份。`--source-data-dir` 可指定含源 `secret.key`
的目录；DPAPI 密文必须在原 Windows 用户身份下解密。迁移窗口内应停止源库写入并保留原始库。

该工具目前只有 SQLite 算法测试，尚未连接真实 PostgreSQL 做端到端导入、恢复演练或应用 smoke test；
它不会备份 PostgreSQL 目标，也不应对现有含其它业务数据的共享服务器运行。

## 老库如何纳入版本管理

已经用 `create_all` 建好的库（有表、没有 `alembic_version`）不能直接
`upgrade head`。正确做法是 `stamp`：

```bash
# 先备份
cp "%LOCALAPPDATA%\aohuang-desktop\app.db" "%LOCALAPPDATA%\aohuang-desktop\app.db.bak"

# 打戳：只写 alembic_version 一行，不动任何业务表
cd server && python scripts/db.py stamp head --real
```

之后这个库就和迁移链对齐了，后续新迁移可以正常 upgrade。

`scripts/db.py check` 的第 [4] 步完整验证了这条路径：

```
[4a] 老库 create_all：exit=0 业务表=18
[4b] 老库直接 upgrade head：exit=1（预期非 0：表已存在）
[4c] 老库 stamp head：exit=0，之后 upgrade head：exit=0，版本表=[('20261030_0912',)]
```

## 加一列的完整流程

见 [CONTRIBUTING.md](../CONTRIBUTING.md) 的「加一个数据库列的完整流程」。
要点重复一遍：

1. 改模型，**必须给 `server_default`**（老库 `ALTER TABLE ADD COLUMN` 需要默认值）
2. `python scripts/db.py revision -m "..."`
3. 确认 `upgrade()` 有 `add_column`、`downgrade()` 有 `drop_column`（在 `batch_alter_table` 块里）
4. `python scripts/db.py check` + `python migrations/_verify_baseline.py`
5. 模型和迁移放同一个 commit

`scripts/db.py` 会在 autogenerate 产出空迁移时自动删掉并提示，避免历史里
堆一串 `upgrade()` 只有 `pass` 的版本号。

## 什么时候必须写迁移

- 改列类型（`String(32)` → `String(64)`）
- 删列、改名
- 加 / 改 / 删唯一约束、外键、索引
- 改默认值语义

以上 `ALTER TABLE ADD COLUMN` 都做不到。**只加一列**时两条路都要：
迁移给新库，`_add_missing_columns()` 给老库。

## CI 里跑什么

`.github/workflows/ci.yml` 的 `python` job（3.11 / 3.12 矩阵）跑一条命令：

```bash
cd server && python scripts/ci_migration_check.py
```

它做的事：

1. 在临时库里 `alembic upgrade head`
2. 把建出的表集合与 `Base.metadata` 比对（不是比数量 —— 那样报错只说
   「少了 1 张」，集合比对能指出是哪张）
3. 比对显式索引数、唯一约束数（含字符串主键产生的 autoindex）、外键数
4. `alembic downgrade base`，断言业务表清空

期望值全部从 `Base.metadata` 派生，**没有硬编码数字**。所以别人加模型时
这条检查不会假红；真红只意味着「迁移漏了」。

**CI 从不碰真实 `app.db`**，也不需要妙手账号。脚本在导入任何项目代码之前
就把 `AOHUANG_DATA_DIR` 覆盖成 `tempfile.mkdtemp()`。

## 常见问题

**`Target database is not up to date.`**
autogenerate 要求目标库已到 head。`scripts/db.py` 在临时库模式下会自动先
`upgrade head`；直接调 alembic 时要自己先 upgrade。

**`table accounts already exists`**
在已存在的库上跑基线迁移。要么用空库，要么先 `stamp head`。

**`UnicodeDecodeError: 'gbk' codec can't decode byte ...`**
`alembic.ini` 里出现了中文。那个文件必须保持 ASCII。

**autogenerate 生成了一条 `drop_table`**
`models/__init__.py` 漏 import 了那个模型，metadata 里没有它。
