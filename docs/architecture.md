# 奥皇上品 · 桌面端架构设计

版本：v1.0（新一代桌面端）
适用代码：`tk-desktop/`
替代对象：`tk-product-automation`（Python 单进程 + http.server + 原生 JS）

---

## 1. 架构总览

### 1.1 进程与连接

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Electron 主进程 (app/)                                                        │
│  · BrowserWindow / 单实例锁 / 原生菜单                                        │
│  · 启动并守护 FastAPI sidecar（uvicorn 127.0.0.1:8812）                        │
│  · 生成并注入本地 token（safeStorage 加密落盘）                                │
│  · electron-updater：检查 / 下载 / 静默安装                                    │
│  · 退出时优雅关停后端（SIGTERM → 5s → kill）                                   │
└───────────────┬──────────────────────────────────────────┬───────────────────┘
                │ contextBridge（preload，无 nodeIntegration）│ spawn + stdout 日志
                ▼                                          ▼
┌───────────────────────────────────┐    ┌─────────────────────────────────────┐
│ 渲染进程 React 18 (web/)           │    │ FastAPI 后端 (server/)               │
│  · Vite + TypeScript + Tailwind    │    │  · REST /api/v1/*（OpenAPI 自动生成） │
│  · TanStack Query：服务端状态       │◄──►│  · 依赖注入：DB session / 当前账号    │
│  · Zustand：界面状态（选择、筛选）   │HTTP│  · 业务服务层（纯 Python，可测）      │
│  · TanStack Table：采集箱 / 仓库     │    │  · 进程内 worker：图片队列 / 定时器   │
│  · shadcn/ui + lucide + sonner     │    │  · 单 worker 进程（SQLite 写锁约束）  │
└───────────────────────────────────┘    └───────┬─────────────┬───────────────┘
                                                 │             │
                                    ┌────────────▼──┐   ┌──────▼──────────────┐
                                    │ SQLite (WAL)  │   │ 本地文件存储          │
                                    │ app.db        │   │ image-results/       │
                                    │ 18 张业务表    │   │ logs/  backups/      │
                                    └───────────────┘   └─────────────────────┘
                                                 │
                        ┌────────────────────────┼──────────────────────────┐
                        ▼                        ▼                          ▼
            ┌───────────────────────┐  ┌──────────────────────┐  ┌──────────────────┐
            │ 妙手开放平台            │  │ 图片翻译 API          │  │ 更新清单           │
            │ openapi-erp.           │  │ image.codesonline.dev │  │ aohuang888.com/   │
            │ 91miaoshou.com         │  │ /v1/images/edits      │  │ .../version.json  │
            │ HMAC-SHA256 签名       │  │ Bearer + 异步 task    │  │                   │
            │ 账号级 1.5s 节流        │  │ 费用台账（10 元上限）  │  │                   │
            └───────────────────────┘  └──────────────────────┘  └──────────────────┘
```

### 1.2 分层职责

| 层 | 目录 | 职责 | 禁止 |
|---|---|---|---|
| 壳 | `app/` | 窗口、单实例、sidecar 生命周期、密钥注入、自动更新 | 不写业务逻辑，不直连 SQLite |
| 界面 | `web/src/` | 展示、交互、乐观更新、错误提示 | 不拼签名、不存 AppSecret、不直连妙手 |
| 传输 | `server/api/routers/` | 参数校验（Pydantic）、鉴权、状态码、错误映射 | 不写业务规则 |
| 业务 | `server/api/services/` | 妙手适配、处理流水线、定价、发布、定时、图片 | 不感知 HTTP |
| 数据 | `server/api/models/` + `db.py` | SQLAlchemy 2.0 模型、事务边界、迁移 | 不含业务判断 |
| 后台 | `server/api/workers/` | 图片任务轮询、定时到点触发、发布结果回收 | 不直接改前端状态 |

### 1.3 运行时事实

- 后端只监听 `127.0.0.1`，端口从 `8812` 起，被占用时递增到 `8822`；Electron 通过 `GET /api/v1/system/health` 确认端口后再加载页面。
- uvicorn `--workers 1`：SQLite 单写者 + 进程内调度器，多 worker 会重复触发定时任务。
- SQLite 开启 `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`foreign_keys=ON`。
- 前端只在 dev 模式连 `http://127.0.0.1:5173`（Vite），打包后加载 `file://` 构建产物，`baseUrl` 由 preload 注入的 `window.__DSH_BOOT__.apiBase` 提供。
- 所有时间戳统一为 Unix 秒（`REAL`），字段名以 `_at` 结尾；日期用 `YYYY-MM-DD` 字符串，时刻用 `HH:MM`。

---

## 2. 目录结构规范

```
tk-desktop/
├─ package.json                 # pnpm workspace 根，脚本：dev / build / dist
├─ pnpm-workspace.yaml
├─ app/                         # Electron 主进程 + 预加载（TypeScript）
│  ├─ package.json
│  ├─ electron-builder.yml
│  ├─ src/
│  │  ├─ main/
│  │  │  ├─ index.ts            # app.whenReady、单实例锁、窗口
│  │  │  ├─ window.ts           # BrowserWindow 尺寸/最小尺寸/安全项
│  │  │  ├─ server.ts           # sidecar 启动、端口探测、健康检查、退出
│  │  │  ├─ token.ts            # token 生成 + safeStorage 加解密
│  │  │  ├─ updater.ts          # electron-updater 封装
│  │  │  └─ menu.ts
│  │  ├─ preload/
│  │  │  └─ index.ts            # contextBridge 暴露 __DSH_BOOT__ / window.api
│  │  └─ shared/
│  │     └─ ipc.ts              # IPC 通道常量与类型
│  └─ resources/                # 图标、安装器资源
├─ web/                         # React 前端
│  ├─ index.html
│  ├─ vite.config.ts
│  ├─ tailwind.config.ts
│  └─ src/
│     ├─ main.tsx
│     ├─ App.tsx
│     ├─ api/
│     │  ├─ client.ts           # fetch 包装：Bearer、错误信封、重试
│     │  ├─ types.ts            # 由 OpenAPI 生成（openapi-typescript）
│     │  └─ queries/            # 每个资源一个 query 模块
│     ├─ pages/
│     │  ├─ Connect/            # 连接妙手账号
│     │  ├─ CollectBox/         # 采集箱列表 + 详情 + 批处理
│     │  ├─ Warehouse/          # 成品仓库（分页/类目/分组/搜索）
│     │  ├─ Publish/            # 发布面板 + 发布记录
│     │  ├─ Schedule/           # 定时任务
│     │  ├─ Words/              # 违规词库
│     │  └─ Settings/           # 版本、更新、数据目录、只读开关
│     ├─ components/
│     │  ├─ ui/                 # shadcn/ui 生成件
│     │  ├─ ProductCard.tsx
│     │  ├─ CategoryTree.tsx
│     │  ├─ GroupChips.tsx
│     │  └─ JobProgress.tsx
│     ├─ hooks/                 # useProducts / useImageJobs / useSchedules
│     ├─ stores/                # Zustand：选择集、筛选、面板开合
│     └─ lib/                   # 格式化、常量、错误文案
├─ server/                      # FastAPI 后端
│  ├─ pyproject.toml
│  ├─ api/
│  │  ├─ main.py                # create_app()、CORS、异常处理器、lifespan
│  │  ├─ cli.py                 # typer：serve / migrate / import-legacy / verify
│  │  ├─ core/
│  │  │  ├─ config.py           # 环境变量 + 数据目录解析
│  │  │  ├─ paths.py            # data/config/runtime 路径（替代 app_paths）
│  │  │  ├─ db.py               # engine / session / WAL pragma
│  │  │  ├─ security.py         # token 校验、共享模式会话、CORS/Origin
│  │  │  ├─ crypto.py           # DPAPI / AES-GCM 账号密钥封装
│  │  │  ├─ errors.py           # AppError 体系 + 错误码常量
│  │  │  ├─ constants.py        # 站点/语言/上限常量（替代 web_domain）
│  │  │  ├─ atomic_io.py        # 原子写（替代 run_records）
│  │  │  └─ service_base.py     # 只读守卫、密钥脱敏
│  │  ├─ models/                # SQLAlchemy 2.0：每表一文件
│  │  ├─ schemas/               # Pydantic v2：请求/响应/分页信封
│  │  ├─ routers/               # auth shops collect_box warehouse groups
│  │  │                         # categories images publish schedules words system
│  │  ├─ services/
│  │  │  ├─ miaoshou/           # client.py signing.py shops.py collect_box.py
│  │  │  ├─ workflow/           # coordinator.py pipeline.py transforms.py
│  │  │  │                      # titles.py diagnostics.py shoe.py
│  │  │  ├─ pricing/            # templates.py calculator.py exchange_rate.py
│  │  │  ├─ images/             # translator.py review.py uploader.py
│  │  │  │                      # rate_limit.py jobs.py
│  │  │  ├─ warehouse/          # store.py service.py source_links.py size_charts.py
│  │  │  ├─ publishing/         # service.py carrier.py ledger.py
│  │  │  ├─ scheduling/         # rules.py runner.py service.py
│  │  │  ├─ categories/         # library.py sync.py
│  │  │  ├─ words/              # service.py matcher.py
│  │  │  └─ system/             # updater.py health.py diagnostics.py
│  │  └─ workers/
│  │     ├─ bus.py              # 进程内任务总线（asyncio.Queue）
│  │     ├─ image_worker.py     # 图片任务提交/轮询/落盘
│  │     └─ schedule_worker.py  # 每秒扫 due，按账号串行
│  ├─ migrations/               # Alembic
│  ├─ tests/
│  └─ scripts/                  # import_legacy.py backfill_accounts.py
├─ docs/                        # 本目录
└─ scripts/                     # 构建与发布脚本
```

**命名约定**

- Python：模块 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE`；一个文件只放一个聚合根。
- TypeScript：组件 `PascalCase.tsx`，hook `useXxx.ts`，API 函数 `verbResource`。
- 路由：资源复数 + kebab-case（`/collect-box`、`/violation-words`、`/publish/logs`）。
- 数据库：表名复数 `snake_case`，外键 `xxx_id`，布尔用 `is_`/`has_` 前缀或语义名（`claimed`、`enabled`）。

---

## 3. 数据模型（SQLAlchemy 2.0 / SQLite）

### 3.1 ER 关系

```
accounts 1───n shops
accounts 1───n products
accounts 1───n schedules
accounts 1───n publish_logs
categories 1───n categories (parent_id)
categories 1───n products
groups     1───n products
products   1───n image_jobs
products   1───n publish_logs
schedules  1───n publish_logs
```

通用约定：

- 主键：业务表用 `INTEGER PRIMARY KEY AUTOINCREMENT`；与上游/哈希绑定的实体用字符串主键（`image_jobs.id`、`schedules.id`）。
- 时间：`*_at REAL`（Unix 秒，浮点，保留毫秒），避免 SQLite 无时区类型带来的歧义。
- 金额：`NUMERIC(12, 2)`；比价用字符串化 Decimal 传前端，避免浮点误差。
- 大对象：`payload`、`images`、`collected` 用 `JSON`（SQLite JSON1），查询只在 `title`、`cid`、`group_id`、`saved_at` 上建索引。
- 软删除：`deleted_at REAL NULL`，默认查询过滤 `deleted_at IS NULL`；图片文件由清理任务按 30 天回收。

### 3.2 accounts（妙手账号）

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | INTEGER | PK | |
| name | VARCHAR(60) | UNIQUE, NOT NULL | 操作者可见的连接名（原 `connectionName`） |
| app_id | VARCHAR(256) | NOT NULL | AppKey |
| app_secret_enc | BLOB | NOT NULL | 密文，永不出现在任何响应里 |
| secret_hint | VARCHAR(8) | NOT NULL DEFAULT '' | 末 4 位，用于界面区分账号 |
| site | VARCHAR(8) | NOT NULL DEFAULT 'MY' | 主站点 |
| is_default | BOOLEAN | NOT NULL DEFAULT 0 | 启动时自动连接 |
| status | VARCHAR(16) | NOT NULL DEFAULT 'active' | `active` / `invalid` / `rate_limited` |
| last_verified_at | REAL | NULL | 最近一次 `shop_list` 成功时间 |
| created_at / updated_at | REAL | NOT NULL | |

索引：`ux_accounts_name(name)`；`ix_accounts_default(is_default)`。

### 3.3 shops（店铺缓存）

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | INTEGER | PK | |
| account_id | INTEGER | FK accounts.id ON DELETE CASCADE | |
| shop_id | VARCHAR(32) | NOT NULL | 平台店铺 id |
| name | VARCHAR(255) | NOT NULL | `platformShopName`/`shopName`/`shopNick` 依次回退 |
| site | VARCHAR(8) | NOT NULL DEFAULT '' | MY/SG/PH/TH/VN/ID |
| site_name | VARCHAR(32) | NOT NULL DEFAULT '' | 中文站点名 |
| status | VARCHAR(32) | NOT NULL DEFAULT '' | 平台原值 |
| synced_at | REAL | NOT NULL | |

唯一约束：`uq_shops_account_shop(account_id, shop_id)`。

### 3.4 categories（类目树）

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | INTEGER | PK | |
| account_id | INTEGER | FK accounts.id ON DELETE SET NULL, NULL | 平台类目树按账号缓存 |
| parent_id | INTEGER | FK categories.id ON DELETE CASCADE, NULL | 根节点为空 |
| cid | VARCHAR(32) | NOT NULL | 妙手 cid |
| name | VARCHAR(255) | NOT NULL DEFAULT '' | 平台英文名 |
| name_zh | VARCHAR(255) | NOT NULL DEFAULT '' | `nameChinese` |
| path | VARCHAR(1024) | NOT NULL DEFAULT '' | `女鞋/运动鞋/跑步鞋` 面包屑 |
| depth | INTEGER | NOT NULL DEFAULT 0 | 0 为根 |
| is_leaf | BOOLEAN | NOT NULL DEFAULT 0 | 只有叶子可保存 |
| sort | INTEGER | NOT NULL DEFAULT 0 | |
| synced_at | REAL | NOT NULL | |

唯一约束：`uq_categories_scope(account_id, cid)`（SQLite 对 NULL 视为不同值，根级用 `account_id=0` 哨兵，见 3.12 约定）。索引：`ix_categories_parent(parent_id)`、`ix_categories_path(path)`。

### 3.5 groups（自定义分组）

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | INTEGER | PK | |
| name | VARCHAR(64) | UNIQUE, NOT NULL | 操作者自建，如「精品」「待改价」 |
| color | VARCHAR(16) | NOT NULL DEFAULT 'slate' | 前端色板键 |
| sort | INTEGER | NOT NULL DEFAULT 0 | 手动排序 |
| created_at / updated_at | REAL | NOT NULL | |

一个商品最多属于一个分组（与旧 `entry.group` 单值语义一致）。需要「一个商品多个标签」时另建 `product_tags`，不在本期范围。

### 3.6 products（成品仓库，核心表）

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | INTEGER | PK | API 里的 `id` |
| warehouse_key | VARCHAR(64) | UNIQUE, NOT NULL | 兼容旧键：`{detail_id}:{shop_id}` |
| account_id | INTEGER | FK accounts.id ON DELETE SET NULL, NULL | 来源账号；detailId 只在账号内唯一 |
| detail_id | VARCHAR(32) | NOT NULL | 妙手采集箱明细 id |
| shop_id | VARCHAR(32) | NOT NULL | 来源店铺 |
| source_shop_id | VARCHAR(32) | NULL | 载体商品改写前的原店铺 |
| title | VARCHAR(512) | NOT NULL DEFAULT '' | 成品标题（已清词、年份已改） |
| language | VARCHAR(8) | NOT NULL DEFAULT '' | ms/en/th/vi/zh |
| site | VARCHAR(8) | NOT NULL DEFAULT '' | |
| cid | VARCHAR(32) | NOT NULL DEFAULT '' | 平台类目 id |
| category_id | INTEGER | FK categories.id ON DELETE SET NULL, NULL | 关联本地类目树 |
| group_id | INTEGER | FK groups.id ON DELETE SET NULL, NULL | 自定义分组 |
| payload | JSON | NOT NULL | 成品 `shopCollectItemInfo` 全量原件，发布时直接复用 |
| images | JSON | NOT NULL DEFAULT '[]' | `imgUrls` 全量，首图为封面 |
| cover_image | VARCHAR(1024) | NOT NULL DEFAULT '' | |
| image_count | INTEGER | NOT NULL DEFAULT 0 | |
| sku_count | INTEGER | NOT NULL DEFAULT 0 | 有效 SKU（`isDelete != '1'`） |
| purchase_price_min | NUMERIC(12,2) | NULL | 采购价下限（`originPrice` 最小值） |
| purchase_price_max | NUMERIC(12,2) | NULL | 采购价上限 |
| purchase_currency | VARCHAR(8) | NOT NULL DEFAULT 'CNY' | |
| stock | INTEGER | NULL | 库存合计 |
| sales_last_30 | INTEGER | NOT NULL DEFAULT 0 | |
| status | VARCHAR(16) | NOT NULL DEFAULT 'ready' | `ready` / `claimed` / `published` / `archived` |
| claimed | BOOLEAN | NOT NULL DEFAULT 0 | 是否已认领进某店铺 |
| claimed_at | REAL | NULL | |
| claimed_shop_id | VARCHAR(32) | NULL | |
| collected | JSON | NOT NULL DEFAULT '{}' | `{accountName: {at, detailId}}`，多账号采集状态 |
| saved_at | REAL | NOT NULL | |
| updated_at | REAL | NOT NULL | |
| deleted_at | REAL | NULL | 软删除 |

唯一约束：`uq_products_identity(account_id, detail_id, shop_id)`；索引：`ix_products_saved(saved_at DESC)`、`ix_products_category(category_id)`、`ix_products_group(group_id)`、`ix_products_status(status)`。
全文检索：`CREATE VIRTUAL TABLE products_fts USING fts5(title, content='products')`，由触发器同步；搜索走 FTS，短查询退化为 `LIKE`。

### 3.7 schedules（定时发布任务）

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | VARCHAR(32) | PK | `task-<uuid12>`，与旧数据兼容 |
| name | VARCHAR(120) | NOT NULL | 默认 `{account} 定时发布` |
| enabled | BOOLEAN | NOT NULL DEFAULT 1 | |
| account_id | INTEGER | FK accounts.id ON DELETE CASCADE | |
| shop_id | VARCHAR(32) | NOT NULL | 必须是该账号下真实存在的店铺 |
| template_name | VARCHAR(64) | NOT NULL | 定价模板名 |
| frequency | VARCHAR(16) | NOT NULL DEFAULT 'daily' | `daily` / `weekly` / `monthly` / `range` |
| days | JSON | NOT NULL DEFAULT '[]' | weekly：1–7 |
| day_of_month | JSON | NOT NULL DEFAULT '[]' | monthly：1–31 |
| start_date | VARCHAR(10) | NOT NULL DEFAULT '' | range 用，含首尾 |
| end_date | VARCHAR(10) | NOT NULL DEFAULT '' | range 用，最多 180 天 |
| time_of_day | VARCHAR(5) | NOT NULL | `HH:MM` 本地时间 |
| per_run | INTEGER | NOT NULL DEFAULT 10 | 1–200 |
| claim_only | BOOLEAN | NOT NULL DEFAULT 0 | 只认领不发布 |
| last_run_date | VARCHAR(10) | NOT NULL DEFAULT '' | 一天只跑一次的判据 |
| attempt_date | VARCHAR(10) | NOT NULL DEFAULT '' | |
| attempts | INTEGER | NOT NULL DEFAULT 0 | 当天失败重试计数，上限 3 |
| next_due_at | REAL | NULL | 预计算，供列表展示与调度扫描 |
| created_at / updated_at | REAL | NOT NULL | |

索引：`ix_schedules_due(next_due_at, enabled)`。

### 3.8 publish_logs（发布记录）

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | INTEGER | PK | |
| account_id | INTEGER | FK accounts.id ON DELETE SET NULL, NULL | |
| product_id | INTEGER | FK products.id ON DELETE SET NULL, NULL | |
| schedule_id | VARCHAR(32) | FK schedules.id ON DELETE SET NULL, NULL | 定时任务触发时填 |
| warehouse_key | VARCHAR(64) | NOT NULL DEFAULT '' | 载体重建后仍能回溯仓库行 |
| detail_id | VARCHAR(32) | NOT NULL | |
| shop_id | VARCHAR(32) | NOT NULL | |
| shop_name | VARCHAR(255) | NOT NULL DEFAULT '' | |
| title | VARCHAR(512) | NOT NULL DEFAULT '' | |
| status | VARCHAR(16) | NOT NULL DEFAULT 'processing' | `processing` / `success` / `fail` / `cancel` |
| reason | VARCHAR(512) | NOT NULL DEFAULT '' | 失败原因（脱敏） |
| platform_item_id | VARCHAR(64) | NULL | 平台商品 id |
| item_edit_url | VARCHAR(1024) | NULL | |
| mode | VARCHAR(16) | NOT NULL DEFAULT 'direct' | `direct` / `carrier` |
| batch_id | VARCHAR(32) | NOT NULL DEFAULT '' | 一次请求的关联 id |
| attempts | INTEGER | NOT NULL DEFAULT 1 | |
| requested_at | REAL | NOT NULL | 本机发起时间 |
| finished_at | REAL | NULL | 平台回执时间 |

唯一约束：`uq_publish_logs_request(account_id, detail_id, shop_id)`（同一账号同一商品重复提交只保留一行并累加 `attempts`）。索引：`ix_publish_logs_time(requested_at DESC)`、`ix_publish_logs_status(status)`。

### 3.9 image_jobs（图片翻译任务）

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | VARCHAR(64) | PK | `sha256(原图 + model + prompt + size)`，天然幂等 |
| batch_id | VARCHAR(32) | NOT NULL DEFAULT '' | 一次批量请求分组 |
| product_id | INTEGER | FK products.id ON DELETE SET NULL, NULL | |
| detail_id | VARCHAR(32) | NOT NULL DEFAULT '' | |
| source | VARCHAR(8) | NOT NULL | `main` / `detail` / `sku` |
| source_url | VARCHAR(1024) | NOT NULL DEFAULT '' | |
| source_sha256 | VARCHAR(64) | NOT NULL DEFAULT '' | |
| status | VARCHAR(16) | NOT NULL DEFAULT 'queued' | `queued` / `submitted` / `ready` / `unknown` / `failed` |
| model | VARCHAR(32) | NOT NULL DEFAULT 'gpt-image-2' | |
| prompt | TEXT | NOT NULL DEFAULT '' | 翻译提示词（换行已规范化） |
| size | VARCHAR(8) | NOT NULL DEFAULT '1:1' | `1:1` / `16:9` / `9:16` |
| reserved_milliyuan | INTEGER | NOT NULL DEFAULT 24 | 预扣费用，最小 24（0.024 元） |
| task_id | VARCHAR(128) | NULL | 上游异步任务 id |
| upstream_status | VARCHAR(32) | NOT NULL DEFAULT '' | |
| filename | VARCHAR(80) | NOT NULL DEFAULT '' | `{id}.png|jpg|webp` |
| message | VARCHAR(512) | NOT NULL DEFAULT '' | 面向操作者的说明 |
| retry_of | VARCHAR(64) | NULL | 重试链起点 |
| attempt | INTEGER | NOT NULL DEFAULT 0 | |
| created_at / updated_at | REAL | NOT NULL | |

索引：`ix_image_jobs_status(status, created_at)`、`ix_image_jobs_product(product_id)`、`ix_image_jobs_batch(batch_id)`。
费用上限：`SUM(reserved_milliyuan)` 的检查放在写事务内（`BEGIN IMMEDIATE`），上限 10000（10 元），超限返回 `IMAGE_BUDGET_EXCEEDED`。旧实现用文件锁，新实现用数据库事务，语义不变。

### 3.10 violation_words（违规词库）

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | INTEGER | PK | |
| word | VARCHAR(64) | NOT NULL | 原文 |
| normalized | VARCHAR(64) | UNIQUE, NOT NULL | `casefold()` 去重键 |
| action | VARCHAR(8) | NOT NULL DEFAULT 'remove' | `remove` / `review` |
| source | VARCHAR(16) | NOT NULL DEFAULT 'manual' | `manual` / `import` / `seed` |
| enabled | BOOLEAN | NOT NULL DEFAULT 1 | |
| created_at | REAL | NOT NULL | |

上限 500 条（`action='remove'`）；`remove` 与 `review` 不允许同词（应用层校验 + 唯一索引兜底）。

### 3.11 settings（键值配置）

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| key | VARCHAR(64) | PK | 点分命名 |
| value | JSON | NOT NULL | 任意结构 |
| updated_at | REAL | NOT NULL | |

已定义的键：

| key | 结构 | 说明 |
|---|---|---|
| `workflow.defaults` | `{languages, mainLimit, detailLimit, concurrency, translateImages, processText, weightIncrement, packageDimensions, sizeChartUrl, pricing, publish}` | 批处理默认值 |
| `pricing.selected` | `{accounts: {accountName: templateName}}` | 每账号记住的模板 |
| `pricing.templates` | `{name: Template}` | 模板库（原 `config/price-templates/`） |
| `publish.enabled` | `bool` | 发布总开关，默认 false |
| `image.limits` | `{minute, hour, day, budgetMilliyuan}` | 图片速率与预算 |
| `update.channel` | `{manifestUrl, channel}` | 更新源 |
| `ui.lastView` | `string` | 上次停留页面（仅桌面端） |
| `security.readOnly` | `bool` | 只读模式（等价旧 `--confirm` 的反面） |

### 3.12 兼容与迁移约定

- `account_id = 0` 作为「全局缓存」哨兵（类目树在没有账号时使用），避免 SQLite 唯一索引对 NULL 的宽松语义。
- 旧 `warehouse.json` 的 `key`、`detailId`、`shopId`、`account`、`group`、`claimed`、`collected`、`payload` 全部保留语义，导入脚本逐字段映射。
- 旧 `runtime/image-generation-ledger.json` 的 `jobs` 直接搬进 `image_jobs`，`reservedMilliyuan` 原值保留（含历史已扣费），保证 10 元上限不被重置。
- 旧 `runtime/publish-ledger.json` 搬进 `publish_logs`，`requestedAt`/`lastRequestedAt` 映射成 `requested_at`，`attempts` 保留。

### 3.13 迁移工具

> **实现现状（以此为准，取代本节的早期设想）**
>
> 早期设计想的是「启动时 `alembic upgrade head`，失败则让 sidecar 退出」。
> 落地时这条路走不通，原因有两条：
>
> 1. 打包后的 sidecar（PyInstaller onedir）里没有 `migrations/` 目录，
>    `alembic` 连迁移脚本都找不到；
> 2. 老用户库的表已经存在（`create_all` 建的），`upgrade head` 会以
>    `table already exists` 失败，等于让所有老用户开不了应用。
>
> 因此实际分工是：
>
> | 场景 | 谁负责 |
> | --- | --- |
> | 新库从零建表 | Alembic（`server/migrations/`） |
> | SQLite 应用启动 | `create_all` + `_add_missing_columns()` |
> | 老库补新列 | `_add_missing_columns()`（`ALTER TABLE ADD COLUMN`） |
> | 改列类型 / 删列 / 改约束 | 只能 Alembic |
>
> 当前模型共有 **18 张表**。SQLite 启动仍通过 `create_all` 和补列逻辑建表；
> PostgreSQL 启动只校验数据库可连接且 Alembic heads 与当前代码一致，不会自动迁移。
>
> 完整用法、老库纳入版本管理的 `stamp` 流程、以及「加一列」的标准步骤见
> [`docs/migrations.md`](migrations.md)。

Alembic 单链式 revision，文件名 `<YYYYMMDD>_<HHMM>_<rev>_<slug>.py`：

- 迁移链从 13 张表的基线继续增加操作员/审计、操作员归属、自动化结果、登录凭据、
  按学员认领、妙手账号归属和自动化运行记录；完整 revision 清单见
  [`docs/migrations.md`](migrations.md)。
- 当前迁移检查在 SQLite 临时库验证出 18 张表、33 个显式索引、18 个自动唯一/主键索引
  和 20 个外键；`python server/scripts/ci_migration_check.py` 会跑 upgrade 与 downgrade。
- 之后每次改表必须提供能真正执行的 `downgrade()`（CI 会跑 `alembic downgrade base` 验一遍）。
- SQLite 应用启动不执行 `alembic upgrade head`；PostgreSQL 启动只校验 revision heads，也不自动迁移。

---

## 4. 关键设计决策

### 4.1 为什么 API-first

1. **复用面**：手机端、Web 端、将来的多店铺值班台都只需要 HTTP + JSON，不依赖 Electron。业务规则全部在 `server/api/services/`，换壳不改逻辑。
2. **可测性**：每个接口可用 `httpx.AsyncClient` 直接测，不必启动 Electron 或浏览器；旧系统把业务写进 `web.py` 的 `BaseHTTPRequestHandler`，测试只能打桩 `HTTPServer`。
3. **契约稳定**：FastAPI 生成 OpenAPI，前端 `types.ts` 由 `openapi-typescript` 生成，字段改名会在编译期报错，而不是运行时白屏。
4. **部署弹性**：同一份代码可以 `uvicorn` 起在操作者机器（桌面模式），也可以起在共享服务器（团队模式），只切换鉴权与绑定地址。
5. **可观测**：中间件统一记录 `request_id`、耗时、错误码；旧系统只有 `log_message`。

代价与约束：所有写操作必须是幂等的或显式带 `Idempotency-Key`；前端不再能直接读 `runtime/*.json`，调试需要走接口或 `server/api/cli.py`。这是有意为之——单一数据入口。

### 4.2 为什么 SQLite

- **本地优先**：单机单用户，全部数据在 `%LOCALAPPDATA%\AOHUANG-Listing\data\app.db`，离线可用，备份就是一个文件（`VACUUM INTO` 生成一致性快照）。
- **量级匹配**：仓库上限 2000 条商品、发布记录万级、图片任务万级，SQLite 在 WAL 下完全够用；旧实现是「每次读写整个 JSON 文件」，2000 条时 gallery 已经要全量反序列化。
- **事务能力**：认领状态、图片预扣费、发布台账都需要「读-改-写」原子性，JSON 文件靠 `.tmp + replace` 只能保证不写坏，保证不了并发互斥。
- **PostgreSQL 支持状态**：可通过 `AOHUANG_DATABASE_URL` 配置 PostgreSQL，启动时校验独立凭据密钥、连接和迁移版本；生产启动不会自动改 schema。受控 SQLite 导入工具已实现并有 SQLite 算法测试，但当前没有真实 PostgreSQL 在线 CRUD、迁移、导入或备份恢复验收。不能把改连接串视为完成数据库切换。
- **已知限制**：单写者。因此后端强制单 worker，写操作串行在短事务里；长任务（图片、发布）只写状态字段，不做长事务。

### 4.3 鉴权方案

**桌面模式（默认）**

| 环节 | 做法 |
|---|---|
| token 生成 | 首次启动 Electron 生成 32 字节 `secrets.token_urlsafe(32)` |
| token 存储 | `safeStorage.encryptString()` 落盘到 `userData/local-token.bin`；同时以 `0600` 写入 `data/token` 供 sidecar 读取 |
| token 传递 | preload 通过 `contextBridge` 注入渲染进程内存，不进 localStorage |
| 请求头 | `Authorization: Bearer <token>`；兼容旧客户端仍接受 `X-Local-Token` |
| 校验 | `secrets.compare_digest`，缺失/不符 → `401 AUTH_REQUIRED` |
| 绑定 | 只监听 `127.0.0.1`；校验 `Host` 为 `127.0.0.1|localhost`，校验 `Origin` 在白名单（`file://`、`http://127.0.0.1:5173`、`http://localhost:5173`） |
| 只读模式 | `security.readOnly=true` 时所有写接口返回 `403 READ_ONLY_MODE`（替代旧 `--confirm`） |

**共享服务器模式（可选）**

| 环节 | 做法 |
|---|---|
| 绑定 | `0.0.0.0:8443`，TLS 由前置 Nginx/Caddy 终止，`X-Forwarded-Proto` 必须为 https |
| 登录 | `POST /auth/login`（账号密码，Argon2id 哈希）→ `POST /auth/connect` 携带用户会话 |
| 会话 | HMAC-SHA256 签名的不透明 token，载荷 `{uid, exp, jti}`，TTL 12h，服务端维护吊销表 |
| 数据隔离 | 所有表带 `owner_id`（本期留空，桌面模式恒为 0）；查询强制注入 `owner_id` 过滤，由 `deps.current_owner` 提供 |
| 妙手凭据 | 只存服务端，加密密钥来自 `AOHUANG_MASTER_KEY`（32 字节 base64），缺省时拒绝以共享模式启动 |
| 禁止 | 不再接受旧系统的 `X-Miaoshou-AppKey` / `X-Miaoshou-AppSecret` 请求头（客户端直传密钥会进日志） |

**账号密钥静态加密**

- Windows：`CryptProtectData`（DPAPI，当前用户作用域），密文与机器绑定。
- macOS：Keychain（`keyring`）保存主密钥，AES-256-GCM 加密字段。
- Linux：`keyring`（Secret Service）；不可用时退化为 `0600` 主密钥文件，并在界面明确提示。
- 任何响应只返回 `secretHint`（末 4 位）与 `appIdHint`（`…1234`）。

### 4.4 长任务与后台执行

| 任务 | 触发 | 执行体 | 状态载体 |
|---|---|---|---|
| 图片翻译 | `POST /images/translate` | `workers/image_worker.py` | `image_jobs.status` |
| 定时发布 | `schedule_worker` 每秒扫 `next_due_at` | `services/scheduling/runner.py` | `publish_logs` + `schedules.attempts` |
| 商品处理（文字+图片+保存） | `POST /collect-box/{id}/process`（附加接口） | `services/workflow/pipeline.py` | 内存 `RunState` + `products` |
| 类目树同步 | `POST /categories/sync` | 同步请求内完成（约 1–3s） | `categories.synced_at` |

规则：

- 接口返回 `202` + 资源 id，前端轮询 `GET /images/jobs/{id}`（1s 起，指数退避到 5s）。
- 进程重启后：`status in (queued, submitted)` 的任务由 `image_worker.reconcile()` 重新查上游，**不重复提交**（与旧 `ImageTranslator.reconcile` 语义一致）。
- 同一账号的上游请求必须经过 `Pacer(1.5s)`；不同账号可并行（实测：同账号 1.0s 仍触发 `accountApiQpsRateLimit`，1.5s 不触发；两账号各 1.5s 并发零失败）。

### 4.5 发布的三重保险

发布不可撤销，新架构保留并强化旧系统的保护：

1. `settings['publish.enabled'] = true`（设置页显式开启，持久化）。
2. 请求体 `"confirm": true`（字面量，不是真值判断）。
3. 必须给出非空 `items[]`，且每项能解析到具体 `detailId` 或 `warehouseId`；不存在「发布整个采集箱」的隐式路径。
4. 站点语言不匹配（如 MY 店铺收到英语/中文外的语言）时，除非 `allowMismatch: true`，否则 `422 SITE_LANGUAGE_MISMATCH`。
5. 每次请求写 `publish_logs`（`processing`），平台回执后更新为 `success`/`fail`。日志只显示本机发起的记录，平台历史（500+ 行手工发布）通过 `platformTotal` 单独暴露。

### 4.6 前端状态分层

- **服务端状态**：TanStack Query。`queryKey` 规范：`['warehouse', {page, categoryId, groupId, q}]`、`['collect-box', {page, status, keyword}]`、`['image-job', id]`、`['shops', accountId]`。写操作 `invalidateQueries` 精确失效，不做全局 refetch。
- **界面状态**：Zustand 三个 store：`useSelectionStore`（采集箱勾选、仓库勾选）、`useFilterStore`（类目/分组/搜索词）、`useUiStore`（面板开合、密度）。选择集按 `warehouseKey` / `detailId:shopId` 存，切页不丢。
- **表单**：受控组件 + Zod 校验，错误信息与后端 `error.code` 对齐，避免两套文案。
- **长任务**：`JobProgress` 订阅 job 轮询结果，展示 `queued/submitted/ready/unknown/failed` 五态与费用小计。

### 4.7 打包与自动更新

- `electron-builder` 出 NSIS 安装器（`oneClick: false`、`perMachine: false`、`CloseApplications: yes`），sidecar 作为 `extraResources` 打进 `resources/server/`，用 PyInstaller `--onedir` 冻结。
- 更新清单沿用 `https://aohuang888.com/tk-product-automation/update/version.json`，字段 `{version, url, sha256, notes, publishedAt}`。下载后先校验 SHA-256，再 `/VERYSILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`。
- `GET /system/version` 同时返回本地版本与清单结果；更新下载由 Electron 主进程负责（渲染进程无文件系统权限），后端只做清单比对，便于共享模式复用。

---

## 5. 从现有 Python 代码迁移的映射

### 5.1 模块级映射

| 旧文件（tk-product-automation/src） | 新位置（tk-desktop/server/api） | 处理方式 |
|---|---|---|
| `app_paths.py` | `core/paths.py` | 保留逻辑：冻结态用 `%LOCALAPPDATA%`，源码态用仓库根；新增 `db_path()`、`backup_dir()` |
| `version.py` | `core/version.py` + `web/src/lib/version.ts` | 版本号单一来源改为 `pyproject.toml`，构建时注入前端 |
| `defaults.py` | `services/workflow/defaults.py` | 原样搬（包裹尺寸、重量步进、尺码图默认 URL） |
| `web_domain.py` | `core/constants.py` + `core/errors.py` | 站点/语言/上限常量与 `EditorError` 拆开；`EditorError` → `AppError` |
| `errors`（散落） | `core/errors.py` | 统一 `AppError(code, message, status, details)` |
| `miaoshou_client.py` | `services/miaoshou/{client,signing,shops,collect_box,publish}.py` | 按资源拆分；`generate_sign` 原样保留（HMAC-SHA256，`secret+path+ts+key+body+secret`）；新增 `Pacer` |
| `credential_store.py` | `services/credentials.py` + `core/crypto.py` | Windows 凭据管理器 → DPAPI/AES-GCM 存 `accounts.app_secret_enc` |
| `transforms.py` | `services/workflow/transforms.py` | 标题清词、年份刷新、SKU 改价、图片删除逐函数搬迁；词库来源改为 `violation_words` 表 |
| `title_editor.py` | `services/workflow/titles.py` | 标题预览/编辑，接口化 |
| `service_title_words.py` | `services/words/service.py` + `routers/violation_words.py` | JSON 文件 → `violation_words` 表；`add/remove/import/remove-many/clear` 动作保留 |
| `category_library.py` | `services/categories/library.py` | `build_category_library` 直接复用（`cateTree` → 扁平 + path） |
| `diagnostics.py` | `services/workflow/diagnostics.py` | `diagnose_item` 的 12 项检查原样保留（缺标题/描述/图/类目/重量/包裹尺寸/SKU/价格/含税价/库存/SKU 重量） |
| `shoe_workflow.py` | `services/workflow/shoe.py` | 颜色归一、尺码、类目兜底；Malay 词典表保留 |
| `price_template.py` | `services/pricing/templates.py` | 模板校验、跨境运费表、汇率解析；存储从 `config/price-templates/*.json` → `settings['pricing.templates']` |
| `pricing.py` | `services/pricing/calculator.py` | 模板选价、`apply_price_template`、展示价 |
| `exchange_rate.py` | `services/pricing/exchange_rate.py` | |
| `image_review.py` | `services/images/review.py` | 翻译提示词、主图/详情图数量上限、OCR 并发、`verify_generated_image` |
| `image_translate.py` | `services/images/translator.py` | 上游提交、task 轮询、`reconcile`、结果落盘；台账改表 |
| `image_upload.py` | `services/images/uploader.py` | 上传翻译结果、失败重试 |
| `rate_limit.py` | `services/images/rate_limit.py` | 滑动窗口从 `image-rate-limit.json` → `settings['image.limits']` + `image_jobs.created_at` 统计 |
| `carrier.py` | `services/publishing/carrier.py` | 公共采集箱取载体、`adapt_payload` 改写 shopId/库存映射、`fill_carriers` |
| `publish_ledger.py` | `services/publishing/ledger.py` | 台账 → `publish_logs`；`requested_ids`/`requested_warehouse_keys` 用 SQL 实现 |
| `warehouse.py` | `services/warehouse/store.py` | `warehouse.json` → `products` 表；`_summary` 的 22 个字段一一对应列 |
| `source_links.py` | `services/warehouse/source_links.py` | 1688 链接池 → `settings` 或独立表（本期放 settings JSON） |
| `size_chart_store.py` | `services/warehouse/size_charts.py` | 尺码图上传/列表/删除 |
| `backfill_warehouse_accounts.py` | `scripts/backfill_accounts.py` | 一次性脚本，导入后按 `account` 回填 `account_id` |
| `product_workflow.py` | `services/workflow/{coordinator,pipeline}.py` | 2108 行按阶段拆：`prepare`（文字）/`images`（图片）/`save`（保存）；`WorkflowCoordinator` 的锁与暂停语义保留 |
| `schedule.py` | `services/scheduling/rules.py` | `normalize_task`、`is_due`、`next_due`、`attempts_today`、重试上限 3 |
| `schedule_runner.py` | `services/scheduling/runner.py` | 按账号串行、`Pacer`、先校验店铺归属、空仓库记 `empty` |
| `service_base.py` | `core/service_base.py` | `required_text`/`require_save`/`redact` |
| `service_warehouse.py` | `services/warehouse/service.py` | gallery/claim/publish_batch/delete/update_category |
| `service_publish.py` | `services/publishing/service.py` | shops/records/publish_confirmed |
| `service_price.py` | `services/pricing/service.py` | 模板 CRUD、预览、同步、选择 |
| `service_schedule.py` | `services/scheduling/service.py` + `workers/schedule_worker.py` | 线程循环 → asyncio 任务 |
| `web.py` | `routers/*` + `main.py` | 见 5.2 |
| `run_records.py` | `core/atomic_io.py` | 仅保留原子写与 JSON 兼容，供导入/日志使用 |
| `prune_runtime.py` | `scripts/prune_runtime.py` | 清理旧 runtime 目录 |
| `update_check.py` | `services/system/updater.py` | 清单比对保留；下载安装移交 Electron |
| `verify_image_api.py` | `scripts/verify_image_api.py` | 图片服务连通性自检 |
| `cli.py` | `cli.py`（typer） | `serve` / `migrate` / `import-legacy` / `verify-image` |
| `web/app.js` | `web/src/pages/{Connect,CollectBox,Warehouse,Publish}/` | 原生 JS 按页面拆分 |
| `web/warehouse.js` | `web/src/pages/Warehouse/` | |
| `web/schedule.js` | `web/src/pages/Schedule/` | |
| `web/index.html` + `style.css` | `web/src/App.tsx` + Tailwind | |

### 5.2 HTTP 路由映射

| 旧接口 | 新接口 |
|---|---|
| `GET /api/bootstrap` | `GET /auth/status` + `GET /shops` + `GET /warehouse`（按需组合） |
| `GET /api/status` / `GET /api/state` | `GET /auth/status` |
| `POST /api/connect` / `POST /api/connect-saved` | `POST /auth/connect` |
| `POST /api/save-connection` / `POST /api/rename-connection` | `POST /auth/accounts` / `PATCH /auth/accounts/{id}` |
| `GET /api/shops` | `GET /shops` |
| `POST /api/scan` | `GET /collect-box`（扫描=分页查询，不再全量拉 100 条） |
| `POST /api/select` | `POST /collect-box/selection`（附加，服务端保存选择集） |
| `POST /api/product-detail` | `GET /collect-box/{detailId}?shopId=` |
| `POST /api/start` | `POST /collect-box/{detailId}/process`（附加） |
| `POST /api/warehouse` | `GET /warehouse` |
| `POST /api/warehouse-detail` | `GET /warehouse/{id}` |
| `POST /api/claim-warehouse-product` | `POST /warehouse/{id}/claim` 或 `POST /collect-box/{id}/claim` |
| `POST /api/update-warehouse-category` | `PATCH /warehouse/{id}` / `POST /warehouse/batch-category` |
| `POST /api/add-warehouse-category` | `POST /groups`（自定义分组） |
| `POST /api/delete-warehouse-product` | `DELETE /warehouse/{id}` |
| `POST /api/publish-warehouse-batch` | `POST /publish`（`items` 用 `warehouseId`） |
| `POST /api/publish` | `POST /publish`（`items` 用 `detailId`） |
| `POST /api/publish-records` | `GET /publish/logs` |
| `POST /api/publish-settings` | `PATCH /settings`（`publish.enabled`） |
| `GET /api/schedule` | `GET /schedules` |
| `POST /api/schedule-save` | `POST /schedules` / `PATCH /schedules/{id}` |
| `POST /api/schedule-delete` | `DELETE /schedules/{id}` |
| `POST /api/schedule-run` | `POST /schedules/{id}/run` |
| `GET /api/title-words` | `GET /violation-words` |
| `POST /api/title-words` | `POST /violation-words` |
| `GET /api/image-service` | `GET /images/service`（附加） |
| `POST /api/image-service/reconcile` | `POST /images/reconcile`（附加；启动时自动执行） |
| `POST /api/update-check` | `GET /system/version` |
| `POST /api/update-install` | `POST /system/update` |
| `POST /api/upload-size-chart` | `POST /size-charts`（multipart，附加） |
| `GET /image-results/{file}` | `GET /images/jobs/{id}/file` |

### 5.3 不可回归的行为清单

迁移验收时逐条对照，任一条不成立即视为回归：

1. 发布必须同时满足：开关开、`confirm=true`、显式商品列表；发布日志只显示本机发起的记录。
2. 图片任务按 `sha256(原图+model+prompt+size)` 幂等，命中 `ready` 直接返回，不重复扣费；`unknown` 状态**不自动重试**。
3. 图片累计预扣费上限 10 元（10000 毫央），写事务内检查。
4. 同一账号上游请求间隔 ≥ 1.5s；不同账号可并行。
5. 标题清理后长度须在 25–255 字符，否则标记 `needsReview`（不阻断，只提示）。
6. 主图软限 9 张、详情图软限 9 张（`workflow.defaults.mainLimit` / `detailLimit`），详情图另有平台硬限 30 张（`DETAIL_IMAGE_HARD_LIMIT`），关闭图片处理时也必须校验硬限。
7. 岗位/类目/尺码图/重量/包裹尺寸缺一不可，`diagnose_item` 的检查项不得减少。
8. 发布到与商品原店铺不同的店铺时，必须走载体重建（`mode=carrier`），并改写 `shopId` 与 SKU 库存映射；同店铺走 `mode=direct`，此时定价模板不生效且必须返回 `warning`。
9. 定时任务一天只跑一次，失败最多 3 次；店铺 id 不属于该账号时整轮失败并给出原因。
10. 定时任务只从仓库取该账号的商品，已发布过的（按 `detailId` 与 `warehouseKey` 双重判重）不再取。
11. 账号密钥永不进入响应、日志、异常文本；错误信息统一脱敏。
12. 只读模式下所有写接口拒绝，包括保存任务、开发布开关、认领、发布。

---

## 6. 关键时序

### 6.1 处理一件商品（文字 → 图片 → 保存 → 入仓）

```
Renderer            Router                Pipeline                Miaoshou        ImageAPI
   │ POST /collect-box/{id}/process        │                        │               │
   ├──────────────────►│ 202 + runId        │                        │               │
   │                   ├───────────────────►│ shop_detail            │               │
   │                   │                    ├───────────────────────►│               │
   │                   │                    │ translate_collect_item │               │
   │                   │                    ├───────────────────────►│               │
   │                   │                    │ 标题清词/年份/重量/包裹尺寸（本地）    │
   │                   │                    │ 图片逐张：命中台账？──否──► 提交翻译 ──►│
   │                   │                    │            │是                            │
   │                   │                    │            └─ 复用已付结果               │
   │                   │                    │ save_shop（ossMd5 必须匹配）            │
   │                   │                    ├───────────────────────►│               │
   │                   │                    │ INSERT products        │               │
   │ GET /runs/{runId} │                    │                        │               │
   ├──────────────────►│                    │                        │               │
```

### 6.2 发布（同店铺 / 跨店铺）

```
POST /publish
  ├─ 校验 publish.enabled + confirm + items 非空
  ├─ 全部 items 的 shopId == 目标 shopId ?
  │    ├─ 是 → client.publish_items(detailIds, [shopId])  → mode=direct
  │    └─ 否 → 逐件 _reprice(template, weight, purchasePrice)
  │            → carrier.collect_carriers(n)
  │            → carrier.fill_carriers(改写 shopId + 库存映射)
  │            → client.publish_items(新 detailIds)        → mode=carrier
  ├─ publish_logs 批量写 processing
  └─ 返回 {mode, published[], failures[], warning?}
```

### 6.3 定时发布

```
schedule_worker（每 1s）
  ├─ SELECT * FROM schedules WHERE enabled=1 AND next_due_at<=now
  ├─ 按 account_id 分组
  ├─ 账号内串行：Pacer(1.5s)
  │    ├─ 校验 shopId 属于该账号
  │    ├─ 取仓库中该账号、未发布、按 saved_at 倒序，最多 per_run 件
  │    ├─ 有模板 → 重算售价；claimOnly → 只认领
  │    ├─ 发布 → 写 publish_logs
  │    └─ 更新 last_run_date / attempts / next_due_at
  └─ 失败：attempts<3 → 当天稍后重试；否则次日再跑
```

---

## 7. 错误模型

统一信封：

```json
{
  "error": {
    "code": "UPSTREAM_RATE_LIMIT",
    "message": "妙手接口限流，请稍后重试",
    "details": {"account": "妙手1234", "retryAfter": 1.5},
    "requestId": "req_01HB7Z3K9Q"
  }
}
```

| HTTP | code | 触发条件 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | 参数缺失/类型错误/长度越界 |
| 400 | `CONFIRM_REQUIRED` | 发布缺 `confirm: true` |
| 401 | `AUTH_REQUIRED` | 无 token |
| 401 | `TOKEN_INVALID` | token 不符 |
| 403 | `FORBIDDEN_ORIGIN` | Origin/Host 不在白名单 |
| 403 | `READ_ONLY_MODE` | 只读模式下的写操作 |
| 403 | `PUBLISH_DISABLED` | `publish.enabled=false` |
| 404 | `NOT_FOUND` | 资源不存在（含软删除） |
| 409 | `CONFLICT_BUSY` | 批次运行中执行互斥操作 |
| 409 | `DUPLICATE_NAME` | 分组名/连接名重复 |
| 409 | `ALREADY_CLAIMED` | 重复认领同一商品 |
| 402 | `IMAGE_BUDGET_EXCEEDED` | 预扣费超过 10 元 |
| 422 | `SITE_LANGUAGE_MISMATCH` | 语言与店铺站点不符且未 `allowMismatch` |
| 422 | `DIGEST_MISMATCH` | 更新包 SHA-256 不符 |
| 502 | `UPSTREAM_ERROR` | 妙手/图片 API 返回业务错误 |
| 502 | `UPSTREAM_UNKNOWN_RESULT` | 图片请求结果未确认（可能已扣费） |
| 503 | `UPSTREAM_RATE_LIMIT` | 妙手限流且重试耗尽 |
| 500 | `INTERNAL_ERROR` | 未捕获异常（响应不含堆栈，日志含 `requestId`） |

前端按 `code` 分支处理，不解析 `message` 文本。

---

## 8. 非功能要求

| 项 | 目标 |
|---|---|
| 启动 | 冷启动到可交互 ≤ 3s；sidecar 健康检查 200ms 间隔，超时 10s 报错并给日志路径 |
| 列表性能 | 仓库 2000 条分页首屏 ≤ 150ms；采集箱单页（20 条）≤ 400ms（含上游 1 次请求） |
| 数据库 | 单文件 ≤ 500MB；每日 `VACUUM INTO backups/app-YYYYMMDD.db`，保留 7 份 |
| 日志 | `logs/server.log` 按 10MB 轮转保留 5 份；结构化 JSON；自动脱敏 AppSecret/token |
| 崩溃 | 未捕获异常写 `logs/crash-*.log`；Electron 捕获 `render-process-gone` 后重载并提示 |
| 离线 | 妙手不可达时，仓库/分组/词库/定时任务仍可读写，列表接口返回 `degraded: true` |
| 并发 | 单实例锁；第二个实例唤起已有窗口，不重复起 sidecar |
| 安全 | `contextIsolation: true`、`nodeIntegration: false`、`sandbox: true`；禁止 `shell.openExternal` 任意 URL（白名单 https） |

---

## 9. 风险与未决项

| 风险 | 影响 | 处置 |
|---|---|---|
| 妙手接口无版本承诺，字段可能变 | 详情解析失败 | 所有上游响应先落 `raw_response` 到日志（脱敏），解析层做键名回退链（旧代码 `_first_text` 思路保留） |
| SQLite 单写者 | 长事务阻塞写 | 事务只包住必要的写；图片/发布只更新状态行 |
| 图片上游“结果未确认” | 重复扣费 | 状态机 `unknown` 禁止自动重试，只能人工确认后显式 `retryOcr` |
| 载体池不足 | 跨店铺发布失败 | 发布前检查可用载体数，不足则整批失败并提示先补池 |
| 共享模式多租户 | 数据越权 | `owner_id` 强制过滤 + 单测覆盖；本期默认关闭共享模式 |
| 旧数据迁移 | 丢历史发布记录 | 导入脚本幂等（按 `warehouse_key`/`detail_id+shop_id` upsert），导入前自动备份 `warehouse.json` |

未决：

1. 是否需要「一个商品多个分组」——当前按单值设计，若运营需要标签化再引入 `product_tags`。
2. 类目树是否按站点分表缓存——当前按 `account_id + cid` 唯一，多站点同 cid 冲突时改为 `(account_id, site, cid)`。
3. 共享模式的计费/配额——本期不做，接口预留 `owner_id`。
