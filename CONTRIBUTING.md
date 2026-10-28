# 贡献指南

## 分支命名

```
main                稳定分支，只接受 PR 合入
feat/<简短描述>      新功能        feat/warehouse-filter
fix/<简短描述>       缺陷修复      fix/schedule-timezone
refactor/<简短描述>  重构          refactor/collect-service
docs/<简短描述>      文档          docs/migrations-guide
chore/<简短描述>     构建/依赖     chore/bump-electron
```

描述用小写连字符，不要中文、不要空格、不要 issue 号前缀。

## 提交信息（Conventional Commits）

```
<type>: <一句话说明>
```

| type | 用途 |
| --- | --- |
| `feat:` | 新功能 |
| `fix:` | 缺陷修复 |
| `refactor:` | 重构（行为不变） |
| `docs:` | 只改文档 |
| `test:` | 只改测试 |
| `chore:` | 构建、依赖、脚本 |

示例：

```
feat: 仓库页支持按分组批量改价
fix: 定时任务在跨天后重复触发
refactor: 把采集逻辑从路由层挪进 collect_service
docs: 补充迁移与 _add_missing_columns 的分工说明
test: 补 settings 默认值的回归用例
chore: 升级 electron 到 31.7
```

正文（可选）写「为什么」而不是「改了什么」—— diff 已经说明了改什么。
涉及破坏性变更时，在 footer 写 `BREAKING CHANGE: <说明>`。

## PR 检查清单

提交 PR 前逐条自查：

- [ ] `pnpm check` 通过（= `pnpm typecheck` + `pnpm lint`）
- [ ] `cd server && python -m pytest tests -q` 通过
- [ ] `cd server && python -m ruff check .` 通过
- [ ] 新功能带测试（`server/tests/` 下，文件名 `test_<模块>.py`）
- [ ] 改了版本号的话，四处都改了，且 `python scripts/check_version.py` 通过
- [ ] 改数据库结构的话，写了 Alembic 迁移（见下节）
- [ ] 没有提交 `*.db`、日志、`secret.key`、真实 AppKey / AppSecret
- [ ] 改动涉及用户可见行为时，同步更新 `docs/`

`mypy` 当前有 44 条存量 error，CI 里是 `continue-on-error`，不阻塞 PR，
但**不要新增**。

## 加一个数据库列的完整流程

这套流程同时维护「新库从零建」和「老库原地升级」两条路，缺一条就会出现
「开发机上好好的，用户升级后报 `no such column`」。

1. **改模型** —— 在 `server/models/<表>.py` 里加列：

   ```python
   memo: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
   ```

   `server_default` 必须有值：老库执行 `ALTER TABLE ADD COLUMN` 时，
   已有行需要一个默认值，否则 SQLite 拒绝执行。

2. **生成迁移** —— 在 `server/` 下：

   ```bash
   python scripts/db.py revision -m "products 加 memo 列"
   ```

   这个脚本默认把目标库指到临时目录，不会误改真实 `app.db`。
   它还会在 autogenerate 产出空迁移时自动删除并提示。

3. **复核迁移文件** —— 打开 `server/migrations/versions/` 下新生成的文件，确认：

   - `upgrade()` 里有 `batch_op.add_column(...)`
   - `downgrade()` 里有 `batch_op.drop_column(...)`（不能是 `pass`）

   SQLite 改列必须走 batch，模板里已经写好了：

   ```python
   with op.batch_alter_table("products") as batch_op:
       batch_op.add_column(sa.Column("memo", sa.Text(), nullable=False, server_default=""))
   ```

4. **验证**：

   ```bash
   cd server
   python scripts/db.py check              # upgrade / downgrade / 无漂移 / 老库 stamp
   python migrations/_verify_baseline.py   # 模型与迁移是否仍等价
   ```

5. **提交** —— 模型改动和迁移文件放**同一个 commit**。分开提交会让
   `git bisect` 时出现「代码引用了不存在的列」的中间态。

6. **不需要动 `_add_missing_columns()`** —— 它读的是模型 metadata，
   模型加了列它自动就兜住了老库。它和 Alembic 的分工见
   [docs/migrations.md](docs/migrations.md)。

### 什么时候必须写迁移（不能只改模型）

- 改列类型（`String(32)` → `String(64)`）
- 删列、改名
- 加 / 改 / 删唯一约束、外键、索引
- 改默认值语义（例如从 `""` 改成 `"active"`）

以上 `ALTER TABLE ADD COLUMN` 都做不到，只有 Alembic 能做。
**只加一列**的情况两条路都需要（迁移给新库，`_add_missing_columns` 给老库）。

## 改版本号

四个文件必须同时改，值完全一致：

| 文件 | 字段 |
| --- | --- |
| `package.json` | 顶层 `version` |
| `web/package.json` | 顶层 `version` |
| `app/package.json` | 顶层 `version`（electron-builder 读它出安装包版本） |
| `server/core/constants.py` | `_package_version()` 里的兜底字面量 |

改完：

```bash
python scripts/check_version.py
```

漏改一处会让「安装包是 0.1.6、界面写 0.1.5」，自动更新逻辑据此判定「有新版本」，
侧栏挂一个永不消失的角标。CI 的 `version-consistency` job 会拦住这种提交。

## 代码风格

- 注释写「为什么」，用中文。写清楚这段代码绕开了什么坑。
- 后端错误用 `core/errors.py` 的工厂函数，不要直接 `HTTPException`。
- 前端 UI 组件从 `@/components/ui` 这个 barrel 导入。
- 不手动重排存量文件（会让 `git blame` 失效）；新文件用编辑器保存时格式化。

## 本地钩子（可选）

想在自己机器上提前拦住问题，把下面内容存成 `.git/hooks/pre-push` 并
`chmod +x`（Windows 下 `git update-index --chmod=+x`）：

```bash
#!/bin/sh
set -e
python scripts/check_version.py
cd server && python -m ruff check . && python -m pytest tests -q
cd .. && pnpm check
```
