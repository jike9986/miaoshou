# 奥皇上品 · 桌面端

妙手 ERP 商品采集 / 批量改价 / 图片处理 / 定时发布的 Windows 桌面工具，
单文件夹装全部：Electron 壳 + React 前端 + FastAPI 后端 + SQLite 本地库。

## 技术栈

| 层 | 选型 |
| --- | --- |
| 桌面壳 | Electron 31 + electron-builder（NSIS 安装程序） |
| 前端 | React 18 + TypeScript + Vite 5 + Tailwind CSS + shadcn/ui (Radix) |
| 数据请求 / 状态 | TanStack Query + Zustand |
| 表格 / 图标 / 提示 | TanStack Table / lucide-react / sonner |
| 后端 | FastAPI + Pydantic v2 + SQLAlchemy 2.0 |
| 数据库 | SQLite（WAL 模式，本地优先） |
| 迁移 | Alembic（`server/migrations/`） |
| 包管理 | pnpm 9 workspace（`app` / `web` 两个包） |
| 运行时 | Python 3.11+ / Node 20+ |

## 目录结构

```
tk-desktop/
├─ app/            Electron 主进程（窗口、壳、自动更新）
├─ web/            React 前端（Vite，构建产物被 Electron 加载）
├─ server/         FastAPI 后端
│  ├─ api/         路由层
│  ├─ services/    业务逻辑（采集、发布、图片、计划任务…）
│  ├─ models/      SQLAlchemy 模型（15 张表）
│  ├─ schemas/     Pydantic 出入参
│  ├─ core/        配置、常量、错误、加解密
│  ├─ db/          engine / session / init_db
│  ├─ migrations/  Alembic 迁移（见 docs/migrations.md）
│  ├─ scripts/     db.py 等后端小工具
│  └─ tests/       pytest
├─ docs/           架构、接口、迁移、截图
├─ scripts/        build.ps1 / deploy.ps1 / 版本校验 / 冒烟
└─ package.json    workspace 根（版本号的唯一来源之一）
```

## 本地开发

前置：

- Node 20+ 与 pnpm 9（`corepack enable && corepack prepare pnpm@9.12.0 --activate`）
- Python 3.11+（CI 跑 3.11 与 3.12 两个版本）

```bash
pnpm install                                  # 一次装全部 workspace
pip install -r server/requirements-dev.txt    # 后端依赖（含 ruff/pytest/mypy）
```

启动：

```bash
pnpm dev          # 前端 + Electron 一起起（后端需另开一个终端）
pnpm dev:server   # FastAPI :8812
```

只跑后端（不装前端也能开发 API）：

```bash
cd server && python -m uvicorn main:app --reload --port 8812
# 接口文档 http://127.0.0.1:8812/api/docs
```

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 开发（前端 + 壳） | `pnpm dev` |
| 只起前端 | `pnpm dev:web` |
| 只起后端 | `pnpm dev:server` |
| 只起 Electron | `pnpm dev:app` |
| 构建前端产物 | `pnpm build:web` |
| 编译 Electron 主进程 | `pnpm build:app` |
| 前端 + 壳一起构建 | `pnpm build` |
| 类型检查（web + app） | `pnpm typecheck` |
| Lint（ESLint，web + app） | `pnpm lint` |
| Lint 自动修复 | `pnpm lint:fix` |
| 类型检查 + Lint | `pnpm check` |
| 格式化 | `pnpm format`（⚠️ 会全量重排 src，见「代码约定」） |
| 只检查格式 | `pnpm format:check` |
| 后端静态检查 | `cd server && python -m ruff check .` |
| 后端测试 | `cd server && python -m pytest tests -q` |
| 后端类型检查 | `cd server && python -m mypy services core` |
| 版本号一致性 | `python scripts/check_version.py` |
| 数据库迁移 | `cd server && python scripts/db.py upgrade` |

## 打包与部署

顺序不能换 —— 前端产物要先进 `resources\web`，后端 exe 要先存在，
electron-builder 才能把它们打进安装包。

```powershell
# 1. 结束占用文件的进程（否则 exe/asar 被锁，复制静默失败）
#    scripts\deploy.ps1 内部会做

# 2. 完整打包（静态检查 → 前端 → PyInstaller → Electron → NSIS）
pwsh -File scripts\build.ps1

# 3. 部署到 C:\AohuangDesktop
#    先删空 resources\web 再整体覆盖，避免新旧 hash 的 js 并存
pwsh -File scripts\deploy.ps1
```

`build.ps1` 的 6 个阶段：静态检查（pyflakes + tsc + 回归）→ 前端 →
PyInstaller → Electron 主进程 → electron-builder → 产物校验。

`deploy.ps1` 只覆盖程序目录，**永远不碰 `userdata\`**（用户数据）。

## 数据目录

| 项 | 值 |
| --- | --- |
| 环境变量 | `AOHUANG_DATA_DIR`（旧名 `TK_AUTOMATION_DATA_DIR` 仍兼容） |
| 默认位置 | `%LOCALAPPDATA%\aohuang-desktop` |
| 部署环境 | `C:\AohuangDesktop\userdata` |
| 数据库 | `<数据目录>\app.db` |
| 密钥 | `<数据目录>\secret.key` |
| 其他 | `images\` `image-results\` `logs\` `backups\` |

`app.db` 与 `secret.key` 是用户资产，**任何脚本、测试、CI 都不许写它们**。
CI 里所有涉及数据库的步骤都把 `AOHUANG_DATA_DIR` 指到临时目录。

## 代码约定

- **注释写「为什么」**，用中文。写清楚这段代码绕开了什么坑、为什么不用更直观
  的写法。不写「这里是循环」这类复述代码的注释。
- **UI 组件统一从 barrel 导入**：`import { Button } from "@/components/ui"`，
  不要 `from "@/components/ui/button"`（barrel 在 `web/src/components/ui/index.ts`）。
- **后端错误统一走 `core/errors.py` 的工厂函数**：
  `raise errors.validation("标题不能为空", field="title")`、
  `errors.not_found(...)`、`errors.upstream(...)`。
  不要直接 `raise HTTPException(...)` —— 工厂函数会带上错误码和字段信息，
  前端靠这些字段定位到具体输入框。
- **版本号改四处**：`package.json`、`web/package.json`、`app/package.json`、
  `server/core/constants.py` 的兜底字面量。改完跑
  `python scripts/check_version.py` 确认（CI 会拦）。
- **提交前跑 `pnpm check`**（= `typecheck` + `lint` + 设计护栏 `design:check`）。
  ESLint 用 flat config（`web/eslint.config.js`、`app/eslint.config.js`），只开
  `typescript-eslint` 的 `recommended`（**非** type-checked），`react-hooks/rules-of-hooks`
  是 error，`exhaustive-deps` 是 warn。
- **新文件的格式用 Prettier**（配置在根 `.prettierrc.json`：双引号 / 分号 /
  `printWidth 100` / 2 空格 / `trailingComma all`），编辑器开「保存时格式化」即可。
  但**存量 71 个 ts/tsx 文件没有全量重排**：实测会动 61 个文件、约 2085 行增删，
  `git blame` 会整片失效。所以 `pnpm format:check` 目前是红的，这是有意为之；
  改老文件时只格式化你实际改的那几行，别整文件重排。**不要**在 `web/` 或 `app/`
  里直接跑 `pnpm format` —— 那会全量重排整个 `src`。将来要做全量重排，单独开一个
  纯格式化 commit，别和功能改动混在一起。

### 禁止事项

- **绝不把 AppKey / AppSecret 写进源码、配置、日志或提交信息**。
  账号凭据只存在用户数据目录，且经 `core/crypto.py` 加密后落库。
- 不把真实 `app.db` 提交进仓库（`.gitignore` 已排除 `*.db`）。
- 不在 `services/` 里直接抛 `HTTPException`（用 `core/errors.py`）。
- 不手改 `server/migrations/versions/` 下已提交的迁移文件。

## 相关文档

| 文档 | 内容 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 分层架构、数据模型、接口约定 |
| [docs/api.md](docs/api.md) | 全部 HTTP 接口 |
| [docs/migrations.md](docs/migrations.md) | Alembic 用法、与 `_add_missing_columns` 的分工 |
| [DESIGN.md](DESIGN.md) | AI/前端共用的设计规则入口与机读 Token 摘要 |
| [docs/design-system.md](docs/design-system.md) | 设计令牌与组件规格 |
| [docs/design-standards-source-review.md](docs/design-standards-source-review.md) | awesome-design-md 来源与适用性评审 |
| [docs/design-audit-2026-10-28.md](docs/design-audit-2026-10-28.md) | 当前 UI 设计审计、修复项与验收基线 |
| [docs/screenshots/](docs/screenshots/README.md) | 截图索引（35 张） |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 分支、提交、PR 检查清单 |
| [docs/HANDOFF.md](docs/HANDOFF.md) | 交接备忘（踩过的坑） |
