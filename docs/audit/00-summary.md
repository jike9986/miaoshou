# 奥皇上品桌面端 · 架构审计总报告

> 审计对象：`D:\缓存文件夹\codex工作台\p006\miaoshou`
> 审计基准：`4c9f32d`（2026-11-26），版本 0.1.54
> 审计方式：**只读 + 实证**。未修改任何源码。每份子报告的关键 P0 都由第二人独立复现过。
> 子报告：
> - [A-data-layer.md](A-data-layer.md) — 数据层与迁移（models / db / 26 个迁移）
> - [B-service-layer.md](B-service-layer.md) — 服务层核心链路（价格 / 认领 / 发布 / 定时）
> - [C-api-auth.md](C-api-auth.md) — API 路由与鉴权（越权矩阵 / 信息泄漏）
> - [D-frontend.md](D-frontend.md) — 前端状态与数据流（web/src，36 349 行）
> - [E-electron.md](E-electron.md) — Electron 主进程与更新链（app/src + 打包）
> - [F-scripts-deploy.md](F-scripts-deploy.md) — 脚本、部署与发布链（scripts / tools / ops）

---

## 一、总览

| 报告 | P0 | P1 | P2 | 合计 | 备注 |
|---|---:|---:|---:|---:|---|
| A 数据层与迁移 | 3 | 6 | 4 | 13 | 全部逐条展开 |
| B 服务层核心链路 | 5 | 9 | 6 | 20 | 全部逐条展开 |
| C API 与鉴权 | 6 | 9 | 10 | 25 | **仅 P0 逐条展开**；P1/P2 只在摘要计数，见下方缺口说明 |
| D 前端状态与数据流 | 5 | 9 | 8 | 22 | 全部逐条展开 |
| E Electron 与更新链 | 6 | 8 | 9 | 23 | 全部逐条展开 |
| F 脚本与部署链 | 6 | 10 | 5 | 21 | 全部逐条展开 |
| **合计** | **31** | **51** | **42** | **124** | |

> ### ⚠ 已知缺口：C 报告的 P1/P2 缺逐条证据
>
> [C-api-auth.md](C-api-auth.md) 的「结论摘要」统计了 **P0 共 6 条、P1 共 9 条、P2 共 10 条**，
> 但正文**只把 6 条 P0 逐条展开了证据**（`### C-P0-1` ~ `### C-P0-6`，含代码原文、实测输出、性质判定、修复方案、回归测试）。
> 19 条 P1/P2 目前只有标题级别的结论（散落在摘要第 4、5 点里，如「错误码契约已破：15 个裸写字符串未登记、`"CONFLICT"` 36 处」），
> **没有逐条的文件:行号证据**。
>
> 因此：**本总报告的 124 条里，可立即据以动手修的是 105 条**（A/B/D/E/F 全部 + C 的 6 条 P0）。
> C 的 19 条 P1/P2 需要补证据后才能进入修复排期。这是本次审计唯一未完成的交付项。

### Lead 独立复核记录（哪些结论被验证、哪些被推翻）

审计员的报告不能直接采信。Lead 对存疑结论逐条独立复跑，结果如下：

| 结论 | 复核方式 | 结果 |
|---|---|---|
| **F-P0-1**「136/157 文件永不上传 → 服务器跑旧代码」 | 连生产逐文件比 sha256 | ❌ **推翻**。12/13 一致；`source_link.py` 的差异是 CRLF（588 行 × 1 字节），文本 diff = 0 行。F 已自行修正为 v2 |
| F 修正后的「服务器 108 个残留文件」 | 全树 diff（本地 284 / 远端 333） | ✅ 成立。含 `bak-20261118122340/` 完整旧版备份目录 |
| F 修正后的「777 目录 + root cron + 666 脚本」 | `stat` + `runuser -u www-data -- test -w` | ✅ 成立。6 个运行目录全 777，`www-data` 可写 666 脚本，root cron 每天跑它 |
| **A-P0-1**「short_id 无唯一约束」 | 三路径脚本 `_verify_a_shortid.py` | ✅ **成立且更精确**：alembic 从零 → **缺列**；create_all 新库 → 有约束；**老库升级 → 列补上但约束补不上**（实测两条 `SAMETO` 都插入成功） |
| **A-P0-2**「CI 三道门禁失效」 | 实跑 `ruff` / `pytest -m unit` | ✅ 成立。ruff exit 1（40 errors）、pytest exit 1（4 failed）。**CI 早就红了** |
| CI 只跑 6% 测试 | `--co` 统计 | ✅ **比报告更严重**：全量 1243 项，CI 只收集 **78 项**，静默排除 **1165 项** |
| **B-P0-1**「入仓 100% 被 422 拦死」 | `_verify_b_p0_1.py` | ✅ **成立**。核心证据：`_is_price_field("_warehousePriceRate") = True`，只加记账键、价格不动 → `prices_unchanged = False` |
| **C-P0-1**「shared_mode 双源分裂」 | `_verify_c_p0_1.py`（实测开关后两源分叉） | ✅ **成立**。界面开共享 → 表源 True / 环境源 False；`deps.py:88`、`:116` 两处鉴权全读环境源 |
| **C-P0-2**「受限分组闸门零接入」 | `_verify_c_p0_2.py`（AST 扫调用点） | ✅ **成立**。`guard_group` **AST 调用点 0 个**；`guard_products` 仅 3 处（claim_job/publish_job/schedule）；`publish_service` 零引用；`warehouse_detail` / `claim_product` 均无闸门 |
| **C-P0-6**「跨租户发布」 | 全仓 `resolve_product` 调用点统计 | ✅ **成立且更广**：23 个调用点，**0 个传 `account_id`**（唯一命中是函数定义）。而 `account_id` 在全仓别处被传了 381 次 → 孤立遗漏而非惯例 |
| **E-P0-1**「version 路径穿越到 Startup」 | 真跑 `path.join` + 实际写文件 | ✅ **成立**。实际写到 `...\Startup\aohuang-Setup.exe` 成功 |

**复核的净收益**：推翻 1 条 P0（F-P0-1 首版），把 2 条从「概数」精确到「可比对的数字」（CI 覆盖 78/1243、`account_id` 0/23），补出 1 个跨模块共同根因（手工白名单，见模式 5）。

> **方法论教训**（已写进 F 报告）：比对服务器与本地代码**必须先统一换行符**，否则 CRLF/LF 会伪装成代码漂移；判断生产状态**必须直接测生产**，不能用本地静态分析代替。

### 性质分布（全体 124 条）

| 性质 | 条数 | 占比 | 含义 |
|---|---:|---:|---|
| **根因** | 38 | 31% | 设计层面缺失，加一行判断补不上 |
| **补丁叠补丁** | 52 | 42% | 在旧错上叠新错，越补越绕 |
| **跷跷板** | 26 | 21% | 修了 A 就必须坏 B，两个真值源互搏 |
| **文档债 / 混合** | 8 | 6% | 文档描述与实现不符，或性质无法单一归类 |

**「补丁叠补丁」占 42% 是这份审计最重要的单一结论。**
它说明这套代码的历史修法一直是「就近加一个 if」，很少有一次把「谁才是唯一权威」重新划清。
后面会看到，同一个业务概念（售价、共享模式、模板选中项、安装包由谁删、清单字段由谁校）
在报告 A / B / C / D / E 里被反复指认为「多处各算一遍」或「两个真值源互搏」——**这不是六个模块各自的毛病，是同一个病在六个地方显形。**

---

## 二、最严重的 12 条（按「能不能直接造成钱的损失或全权控制」排序）

> 这 12 条已全部由第二人独立复现。每条给出：一句话结论 + 关键证据 + 性质 + 修复入口。

### 第 1 梯队：拿到就能全权控制，或直接算错钱

#### ① 共享部署下本机 `secret.key` 仍是管理员凭据（`shared_mode` 双源分裂）
**C-P0-1** · 性质：**跷跷板**（两个真值源互搏）

`services/share_service.py:141-157` 的 `shared_mode(db)` 优先读 settings 表；而鉴权判断在 `api/deps.py:88` 与 `:116` 直接读 `Settings.shared_mode`（`core/config.py:84`，只读环境变量，且 `get_settings()` 带 `@lru_cache`）。
界面把开关写进 settings 表 → 鉴权读不到 → **本机 secret.key 仍然被当作管理员**。
`core/config.py:82` 的注释还写着「它**不是**权限开关：没有任何鉴权逻辑读它」——**注释与代码互相打脸**。

- 影响：任何「多人共用一台服务器」的部署，只要没设 `AOHUANG_SHARED_MODE=1`，每个能连上的客户端都持管理员权限。
- 修复入口：`api/deps.py:88/116` 改为调用 `share_service.shared_mode(db)`（唯一权威）；`core/config.py:82` 注释同步改正。

#### ② 跨站点认领时，成本线抬价用「采集地」汇率算「目标站点」价格 —— 38.41 MYR 被写成 259.71 MYR
**B-P0-2** · 性质：**根因**

`services/carrier_service.py:164-165`
```python
    site = str(payload.get("site") or "").strip().upper()
    rate = _claim_site_rate(site)
```
`site` 是**采集地**（上游 `shopCollectItemInfo.site` 带下来的），不是目标店铺。而 `fill_carriers(...)` 的签名里根本没有「目标站点」这个入参，所以函数只能取采集地。

实证（采集 PH → 目标 MY，MY 正确价已折好 16.70）：
```
payload.site = PH
抬价前 price = 16.70
妙手报错     = 本地展示价16.70MYR，无法覆盖产品基础成本 38.40MYR
抬价后 price = 259.71        ← 应为 38.41，实际 6.76 倍
usedFloor    = False          ← 连「抬到成本线」都没做
```
反向（采集 MY → 目标 PH）则返回 `None`，认领永久失败。

- 影响：跨站点认领（MY↔PH/TH/VN/SG）一旦触发成本线，要么挂 6 倍价（卖不动 + 定价污染），要么永久失败。
- 修复入口：`fill_carriers` / `collect_and_fill_carriers` / `_raise_price_to_reported_floor` 全部显式接收 `target_shop_site`，由 `publish_service.py:1200`（那里已有 `target_shop["site"]`）传下去。

#### ③ 入仓价格改写让「没开定价就不许改价」变成恒假 —— 每次入仓必被 422 拦死
**B-P0-1** · 性质：**根因**

这条让「自动化处理 → 入仓」主链路**在规则层必然返回 422**。而现有测试全部绕过了 `enforce_pre_save=True`，所以 CI 是绿的。
→ 详见 [B-service-layer.md](B-service-layer.md) B-P0-1。

#### ④ 跨租户发布：学员 A 的私有商品可用学员 B 的账号发出去
**C-P0-6** · 性质：**根因**

`warehouse_service.resolve_product()` 的 `account_id` 形参（`services/warehouse_service.py:653`）**全仓零调用方传**（20 处调用全部省略）。发布路径 `services/publish_service.py:536` 与 `:560-562` 两条分支都不校验商品归属：
```python
# :536
                product = warehouse_service.resolve_product(db, warehouse_id)
# :560-562  detailId 分支是裸 ORM
        product = (db.query(Product)
                   .filter(Product.warehouse_key == key, Product.deleted_at.is_(None))
                   .one_or_none())
```

- 影响：水平越权。学员能把自己私有商品挂到别人账号/店铺下发布。
- 修复入口：`resolve_product` 的 `account_id` 从「可选」改成「必传」；`publish_service` 两条分支都传并对不上直接 403。

### 第 2 梯队：生产功能直接不可用

#### ⑤ `operators.short_id` 有模型无迁移 —— PostgreSQL 部署 100% 起不来
**A-P0-1** · 性质：**根因**

`models/operator.py:57` 声明 `short_id = mapped_column(String(8), unique=True)`，26 个迁移**零命中**。引入它的 commit `8cfffe9`（v0.1.21）只带了 `20261114_0001`（multi_device + operator_tokens），漏了 short_id。

- PostgreSQL 路径（`db/session.py:646-652` 只校验 head，不跑 `_add_missing_columns`）→ `OperationalError: no such column: operators.short_id`。9 处 `serialize()` 调用点全部触雷，**登录流程本身就崩**。
- 桌面 SQLite 路径被 `_add_missing_columns` 补了列，但**补不上唯一约束** —— 实测两条 `short_id='123456'` 都能插入成功。
- `tests/` 对 short_id **零覆盖**。

- 修复入口：补迁移（列 + `uq_operators_short_id` 部分唯一索引）+ 在 `db/session.py` 加第 4 个 `_ensure_sqlite_*` 补偿函数（前 3 个分别是 username / email / app_key，说明团队知道这个坑，只是漏了第 4 个）。

#### ⑥ `manifest.version` 零校验参与路径拼接 —— 可写任意 `*-Setup.exe` 到 Startup 目录（开机自启）
**E-P0-1** · 性质：**根因**

`app/src/updateInstaller.ts:299`
```ts
  const target = path.join(dir, `AohuangShangpin-${manifest.version}-Setup.exe`);
```
同一函数对 `url` 有白名单（`:285-295`）、对 `sha256` 有正则（`:293-295`），**唯独被拿去拼路径的 `version` 零校验**。

实测：
```
version = "99..\\..\\..\\..\\..\\..\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\aohuang"
→ C:\Users\Administrator\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\aohuang-Setup.exe
仍在 updates/ 内: false
```
`is_newer()`（`server/services/system_service.py:42-53`）只取每段里的数字，`"99..\.."` 解析成 `(99,)`，服务端闸门直接放行。

- 影响：**本次审计唯一的「写任意路径 + 开机自启」原语。** 触发前提是能控制 `version.json`（或 CDN 上的文件）——门槛远低于控制已签名安装包的字节。
- 修复入口：`downloadInstaller()` 入口加 `/^[0-9]{1,4}(\.[0-9]{1,4}){1,3}$/` 校验；`pendingInstaller()` 里对 `data.path` 做 `startsWith(updatesDir() + sep)` 断言。

#### ⑦ 完整性锚点被渲染层持有 + 安装前不重校验
**E-P0-2** · 性质：**根因**

`sha256` 只由渲染层传入（`app/src/preload.ts:28-34` → `main.ts:376-393`），不是服务端独立取；下载完写进 `pending.json`，但 `pendingInstaller()`（`updateInstaller.ts:262-273`）安装时**只 `fs.access` 存在性，不重校验**。`pending.json` 里的 `sha256` 字段**没有任何读者**。
整条链的完整性 = 「渲染层源码没被改」+「TLS 没被破」，而 `resources/web/` 是本机明文可改的目录。

### 第 3 梯队：门禁与测试基础设施失效（让上面所有问题更难被发现）

#### ⑧ CI 三道门禁同时失效 —— 「测试是绿的」这个前提不存在
**A-P0-2** · 性质：**根因（治理层）**

| 检查项 | 命令 | 实测结果 |
|---|---|---|
| ruff | `python -m ruff check .` | **40 errors** → fail |
| pytest（CI 范围） | `python -m pytest tests -m unit -q` | **4 failed / 78** → fail |
| 迁移冒烟 | `python scripts/ci_migration_check.py` | exit 0，但**把真实漂移降级成 warning 放行** |
| 逐列漂移（正确检出） | `python migrations/_verify_baseline.py` | **exit 1** —— 但**不在 CI 里** |

pytest 失败原因：`DEFAULT_FREIGHT_TABLE` 在 `70086d6` 从 0.18/0.12 改成 0.06/0.08（`services/price_template.py:19-25`），测试仍硬编码旧值（`tests/test_price_template.py:26-47`）。

更关键的是 **CI 只跑 `-m unit`，该集合只有 6 个文件**：
```
tests/test_collect_box_service.py: 9
tests/test_price_template.py: 17
tests/test_schedule_multi_shop.py: 9
tests/test_schedule_service.py: 16
tests/test_site_charge_fields.py: 17
tests/test_warehouse_service.py: 10
```
`test_role_permissions.py`（鉴权矩阵）、`test_account_ownership.py`（多租户隔离）、`test_shared_account_task_isolation.py` —— **这三个真正防越权的测试全部不在 CI 里**（`server/pyproject.toml:92` 注释直说 `"CI only runs these"`）。

#### ⑧-bis 96 个失败的逐个归类（Lead 独立复核，可复跑）

A 报告了「CI 范围内 4 failed / 78」，但**全量是 96 个失败**。这个数字必须拆开看，否则会被误读成「96 个真 bug」而吓住，或者被误读成「全是测试烂」而放过真问题。

复跑命令：`cd server && python -X utf8 ../docs/audit/_triage_tests.py`

| 类别 | 数量 | 判定 | 处置 |
|---|---:|---|---|
| **TEST_STALE**：生产签名/口径正确，测试没跟上 | **49** | 不是 bug，是**测试债** | 改测试 |
| ├ `fill_carriers() got an unexpected keyword argument 'category_trees'` | 38 | `154a21d`(11-03) 给生产加了参数，测试桩没加，**烂了 23 天** | 桩加参数 |
| ├ `<lambda>() got an unexpected keyword argument 'operator_id'` | 7 | 多租户隔离改造后测试 lambda 桩没跟上 | 桩加参数 |
| └ 马来运费 `0.18→0.06` / 续重 `0.12→0.08` | 4 | `70086d6`(11-18) 按妙手官方运价改，测试硬编码旧值 | 改期望值 |
| **ENV**：`fixture 'probe_runner' not found` | **20** | `test_parity.py` 依赖一个**从未存在于 conftest 的 fixture** | 补 fixture 或标 skip |
| **PROD_BUG**：测试是对的，生产行为错了 | **1** | ★ `verify_readback is True` 断言失败 | 见下 |
| **待人工判定** | **26** | 多为断言文案/计数口径变化 | 逐条看 |

**唯一被证实是「测试对、生产错」的那 1 条，值得单独说**：

```python
# tests/test_student_publish.py:1120-1121（测试的契约）
assert verify_readback is True, "发布路径必须开启读回核验"
assert strict_readback is True, "发布路径必须是严格核验（含价格/类目）"

# services/publish_service.py:1547（生产实际）
verify_readback=False,   # 负责人 2026-11-18 口径关闭
```

git 时间线证明这是一次**静默的行为回退**：
- `ff5178f`（2026-11-07）标题就叫「fix: **发布前强制读回核验**」——建立契约并写了断言
- `fd826fa`（2026-11-19）把它改成 `False`，而该 commit 的 message 里**一个字都没提关闭读回核验**（它讲的是发布任务化/五国定价/节流优化）

`publish_service.py:1534-1535` 有一段注释解释理由（「仓库商品在入仓阶段已完整质检，发布时再逐件拉回详情核对属于重复劳动」），说明**决策是有意的，但传达过程断了**：改了行为、没改测试、没写进 commit message、没更新 `rules.md`。

> **这一条的正确处置不是「改测试让它过」**，而是：确认 11-18 的口径是否仍然有效 → 有效就把测试断言改成 `assert verify_readback is False` **并在注释里写明依据和日期**；无效就改回 `True`。**无论哪种，都必须让测试和意图一致** —— 现在这种「测试说要、代码说不要、谁都没发现」的状态，比任何一边单独错都危险。

**结论：96 个失败里，真正指向生产 bug 的是 1 个（且是行为契约问题），49 个是测试债，20 个是环境缺失，26 个待判定。** 但这个分布本身暴露了更严重的事：**测试套件已经失去了「红灯 = 有 bug」的信号价值** —— 96 个红灯里 69 个是噪音，没人会再从里面找那 1 个真的。

- 修复入口：先修 4 个运费测试 + `ruff --fix`；把 `_verify_baseline.py` 提进 CI 替换/叠加 `ci_migration_check.py`；扩大 `-m unit` 覆盖到鉴权/多租户三个文件。

#### ⑨ 「改密码」这道门只挡住了老实人
**C-P0** · 性质：**补丁叠补丁**

`_guarded`（`api/__init__.py:50-53`）一次挂三道依赖、全子树继承，是教科书级正确做法。
独立实测（遍历 `app.routes` 的依赖树，统计不含 `require_token` / `current_operator` 的端点）：
```
API 路由数: 183
无 token 依赖的端点: 11
   GET  /api/v1/system/health
   GET  /api/v1/system/share/health
   POST /api/v1/auth/login
   POST /api/v1/auth/email-code-login/request
   POST /api/v1/auth/email-code-login/confirm
   POST /api/v1/auth/register
   POST /api/v1/auth/registration-code/request
   POST /api/v1/auth/password-reset/request
   POST /api/v1/auth/password-reset/confirm
   GET  /api/v1/auth/credentials-status
   POST /api/v1/auth/initialize-credentials
```
—— **183 个端点里 172 个强制鉴权，11 个无 token 的全部是有意公开的登录/注册/健康检查**。这一层确实没有漏。

但 `operators.router` / `operators.audit_router` / `system.router` 绕开 `_guarded` 单独挂载（`:72-73`、`:79-82`），前两者**丢了第三道 `require_password_change_complete`**。
实测：`must_change_password=True` 的老师能 `POST /operators`、`PATCH /operators/{id}`、`POST /operators/{id}/rotate`（抢 token），而同一个 token 打 `/warehouse` 是 **403**。
**鉴权链本身没漏，漏的是「改密码」这第三道防线——它只挡住了老实人。**

#### ⑩ 受限分组的闸门只装在列表页 —— 学员能读、能认领「精品」组商品
**C-P0-2** · 性质：**根因**

`group_access.guard_group`（`services/group_access.py:106`）**全仓零调用者**。`guard_products` 只接了 3 处（`claim_job_service.py:133`、`publish_job_service.py:184`、`schedule_service.py:610`），**`publish_service.py` 一处都没有**。
实测学员能 `GET /warehouse/{id}?includePayload=1` 读到「精品」组商品全文、能 `POST /warehouse/{id}/claim` 认领它。

- 影响：负责人口径是「这三组拿货价是标高的，学员按它发出去是卖一件亏一件」。列表页挡住了，详情页和认领接口没挡。**亏钱是直接结果，不是风险。**

#### ⑪ 定价模板「选中项」存在全局键里 —— 老师选的贵模板串到学员界面，且学员无权改回
**C-P0-3** · 性质：**跷跷板**

模板**库**已按 `operator.id` 隔离（`_templates_key`，`services/setting_service.py:136-140`，正确），但「当前选中哪个模板」仍存在全局 `PRICING_SELECTED_KEY`（`services/setting_service.py:261-264` 的 `selected_templates(db)` 不带 operator）。
`POST /templates/select` 需要 `process` 权限 → 学员**被锁在一个改不掉的错误选中项上**。模板决定售价。

#### ⑫ 仓库根 `.captcha_dom.txt`（936 KB）被 git 跟踪，含可重放的 `x5secdata`
**A-P0-3** · 性质：**根因**

`.gitignore:94-95` 排除的是 `tools/source_match/.captcha_dom.txt`，**不是仓库根那份**。实测内容：
```
x5secdata=xg32a3de2e590ecfc7jaa8d820f8cd1e317d496b14ece58ff5281795521507a-...__bx__s.1688.com%3A443%2Fselloffer%2Foffer_search.h
```
带完整目标路径，是从真实已通过验证的会话里 dump 出来的。同类文件仓库根还有 9 个（`.kw_page.html` 419 KB、`.c3_page.html` 223 KB 等）。

- 修复入口：`.gitignore` 改按文件名模式全局匹配（`.captcha_*` / `.kw_*` / `.img*` / `.diag.*`）；`git rm --cached`；凭据已进历史需 `git filter-repo`；1688 侧重新登录作废该会话。

---

## 三、跨报告的系统性模式（这比单条 P0 更值得决策）

审计六份子报告独立进行，但在**四个模式**上高度收敛。这些是「改一处补不上」的结构性问题。

### 模式 1：同一个业务概念被多处各算一遍（42% 补丁叠补丁的来源）

| 概念 | 实现处数 | 报告 |
|---|---|---|
| 「本地售价」 | **4 套实现 / 3 个汇率来源 / 3 种数据类型** | B-P0-4 |
| 「重量该是多少」 | 3 处（`product_standard` 裁剪 / `publish_service` 品类 cap / `price_template` 计费） | A-P1-4 |
| 「共享模式」 | 2 个真值源 | C-P0-1 |
| 「模板选中项」 | 库按人隔离，选中项是全局 | C-P0-3 |
| 「清单字段该由谁校验」 | 客户端 / 服务端 / 渲染层三套 | E-P1-8 |
| 「安装包该由谁删」 | 3 套机制 | E-P1-5 |

**这是 42% 的直接解释**：新修法总是在已有实现旁再加一份，而不是替换。
`rules.md` 里那条 Pacer 教训（「MIN 必须严格小于默认值，否则 reward 形同虚设」）和「批量×逐件」教训，都是同一个病的历史病例。

### 模式 2：两个真值源互搏（21% 跷跷板的来源）

- `shared_mode`：settings 表 vs 环境变量（C-P0-1）
- `localStorage` 参数 vs 服务端参数（D：`loadSettings`/`saveSettings` 绕过 TanStack Query，靠 800ms 防抖 + `syncedFromServer` ref 双向同步）
- 迁移漂移：Alembic 链 vs `_ensure_sqlite_*` 补偿函数 —— **9 个补偿函数 vs 8 条迁移，没有对照表**。`20261114_0001` 弄丢了 `uq_operators_email_normalized`，桌面端靠补偿函数救回来，PostgreSQL 没有补偿函数 → **生产库邮箱唯一性已丢失**（A-P1-1 证据 4）。这是跷跷板最具体的形态。

### 模式 3：校验写对了，但没站在必经之路上

- `verify_release.py` 能挡住固定链接事故，但 `scripts/build.ps1` **从头到尾不调用它**（全仓无一处引用）（E）
- `verify-desktop-package.mjs` 只比版本号，**不比 `dist/*.js` 与 `src/*.ts` 的对应关系** → 已删除的 `app/dist/installerArgs.js`（Inno `/VERYSILENT` 死代码）仍在 `app.asar` 里随包分发（E）
- `_verify_baseline.py` 是**正确的检查器**，docstring 却写着「不需要进 CI」；CI 里跑的是只数数量、把差异降级成 warning 的版本（A-P1-1）
- `guard_group` 写好了、注释漂亮、**零调用**（C-P0-2）

### 模式 4：架构文档描述不存在的实现

`docs/architecture.md` 三处写「单实例锁」（L16/L54/L760）、L20 写「退出时优雅关停后端（SIGTERM → 5s → kill）」、L19 写「electron-updater」、L484 写「落盘到 `userData/local-token.bin`」。
实测：全仓 `requestSingleInstanceLock` / `second-instance` / `SIGTERM` / `taskkill` / `getppid` **命中数为 0**，`app.asar` 内 `dist/main.js` 同样为 0；`token.ts` / `updater.ts` / `index.ts` 三个文件不存在。

**这不是「文档没更新」，是文档把不存在的实现描述成了已有能力。** 它会让下一个人（和下一个 AI）在错误的基线上做决策 —— 本审计任务书里「注意它可能已过时」这句提醒，被证实是对的，且比预期的更严重。

---

### 模式 5（Lead 独立复核新增）：手工白名单 —— 42% 「补丁叠补丁」的共同物理载体

上面四条模式都在描述「同一个概念有多份实现」。进一步追问**为什么总是变成多份**，答案高度一致：
**因为每次都用「手工列表」来表达「哪些算 X」，而手工列表必然漏，漏了就在旁边再加一个手工列表。**

这是本次审计里唯一可以量化、且横跨全部六个模块的单一根因。Lead 独立复核时找到的实例：

| 手工列表 | 位置 | 漏了什么 | 后果 |
|---|---|---|---|
| `_is_price_field` 用**子串匹配** | `pre_save_check.py:120` `"price" in key.casefold()` | 把本地记账键 `_warehousePriceRate` 判成业务价格字段 | **入仓 100% 被 422 拦死**（B-P0-1）。实测：价格数字一个字没改，只多了记账键 → `prices_unchanged = False` |
| `_LOCAL_ONLY_PAYLOAD_FIELDS` 手工白名单 | `warehouse_service.py:4138` | 漏了 `_warehousePriceRate`（同样含 price 字样） | 本地记账键被推给上游妙手 |
| `_PRICE_FIELDS` 显式白名单 | `readback_check.py:26` | ——（**这是正确写法**） | 同一个「价格字段」概念，`readback_check` 用白名单、`pre_save_check` 用子串匹配，**两套口径** |
| `deploy_server.py` 的 `FILES` | `scripts/deploy_server.py:11-37` | 158 个文件里只列 21 个 | 新增文件默认不部署（F-P1-10） |
| `ALLOWED_HOSTS` | `app/src/updateDownload.ts:10` | 5 个真实主机名只列 2 个 | 校验盲区（F-P1-1） |
| `API_ERROR_TEXT` | `web/src/lib/api.ts:140` | 后端 37 个错误码只覆盖 11 个 | 27 个错误无文案 → 用户看到「操作失败」（D-P1-1） |
| 9 个 `_ensure_sqlite_*` 补偿函数 | `db/session.py` | 漏了第 4 个（`short_id`） | 老库升级后唯一约束永久缺失（A-P0-1） |
| `DYNAMIC_WRITE_ROUTES` 测试白名单 | `tests/test_role_permissions.py` | 漏了 `system/templates` | 结构性守护变红（C） |

**共同形状**：列表是**手工维护**的 → 新增成员时没人想起它 → 漏 → 现象出现 → **在漏的地方再加一个手工列表**，而不是把「哪些算 X」变成**由数据结构自身推导**。

**根因修复的方向不是「补全列表」，而是「消除列表」**：

- 价格字段：`pre_save_check` 直接复用 `readback_check._PRICE_FIELDS`（唯一权威），并把它升格为**上游协议的一部分**（写进 `docs/CLAIM_STANDARD.md`），而不是散在两个文件里各写一份
- 本地字段：`_LOCAL_ONLY_PAYLOAD_FIELDS` 改为「凡以 `_` 开头的一律本地」，前缀即约定，无需枚举
- 部署清单：按目录 `rglob` 推导 + 排除规则，不按文件名枚举（F 已给代码）
- 错误文案：从后端 `errors.py` 的枚举**生成**前端映射，不手抄
- 迁移补偿：建「补偿函数 ↔ 迁移」对照表 + 测试断言两边同步（A-P2-1）

> 这条模式的实践含义：**以后每次想写一个 `SOME_FIELDS = (...)` 常量时，先问「能不能从别处推导出来」。** 能推导就不许枚举 —— 枚举的每一份都是一次未来的漏。

---

## 四、优先修复顺序（按「风险 / 成本」比排序）

### 立刻（今天，都是小改动）
1. **修 CI**：4 个运费测试对齐 0.06/0.08 + `ruff --fix`（24 个可自动修）。**不修这个，后面所有修复都无法验证。**
2. **`.gitignore` 改文件名模式** + `git rm --cached` 那 10 个转储文件。1688 侧重新登录作废会话。
3. **`errors.INTERNAL` → `errors.INTERNAL_ERROR`**（`api/system.py:760`）。当前一旦 `/system/logs/tail` 读文件失败就是 `AttributeError` → 兜底 500（C-P0 已实证该常量不存在）。

### 本周（需改代码 + 加迁移）
4. **`short_id` 补迁移 + 补偿函数 + 测试**（A-P0-1）。这是「生产起不来」级别。
5. **`shared_mode` 统一到 `share_service.shared_mode(db)`**（C-P0-1）。这是「人人管理员」级别。
6. **`version` 路径校验 + `pendingInstaller` 路径断言**（E-P0-1）。这是唯一能持久化到开机自启的原语。
7. **`resolve_product` 的 `account_id` 改必传**（C-P0-6）。

### 本月（需重新划定权威）
8. **把「币种由目标店决定」定下来**（B-P0-2）：全链路传 `target_shop_site`。
9. **收拢「本地售价」到单一实现**（B-P0-4）：这是 B-P0-2/B-P0-3 的共同根。
10. **把 `_verify_baseline.py` 提进 CI**，替换 `ci_migration_check.py` 的数量比对。补一条迁移重建 `uq_operators_email_normalized`。
11. **扩大 CI 覆盖面**到鉴权/多租户三个测试文件。
12. **`weight` / `payload.sourceUrl` / `quality_review.verifiedRevision` 升为真实列**（A-P1-3/A-P1-4）。这三个是「把可筛选字段塞进 JSON 导致物理上无法索引」，加索引治不了本。
13. **修 `docs/architecture.md` 的四处不实描述**（单实例锁 / 优雅关停 / electron-updater / local-token.bin）。

### 需要决策（架构级）
14. **确立单一事务契约**：服务层**永不** commit，commit 只允许在 `api/` 与后台任务入口出现。当前 `warehouse_service.py`/`carrier_service.py` 是 0 commit（对的），`claim_job_service.py`(9) / `publish_job_service.py`(8) / `automation_run_service.py`(7) 自己 commit（导致上层无法原子组合，`unknown_message="请先在采集箱核对"` 就是被迫的降级）——**同一目录两种契约并存**（A-P1-6）。
15. **建立「补偿函数 ↔ 迁移」对照表**并加测试（A-P2-1）。9 对逻辑写两遍是漂移温床。
16. **拆 `web/src/lib/queries.ts`**（2 637 行 God File：56 个内联类型 + 103 个 hook + HTTP client + 业务常量），并合并 `types.ts`（1 640 行）里已开始重复的定义（D）。

---

## 五、做得对的地方（不要在重构时改坏）

审计的价值不只是列问题。以下是有意识的设计，不是运气：

| 位置 | 做法 | 为什么好 |
|---|---|---|
| `api/__init__.py:50-53` | `_guarded` router 一次挂三道依赖，全子树继承 | 新加模块**不可能**忘记鉴权；183 端点里 172 强制鉴权，11 个无 token 的全部有意公开 |
| `api/deps.py:236-246` | `require_permission` 工厂 + `checker.permission` 属性 | 让**测试能从依赖树读实际权限**，而不是靠 grep 装饰器文本 |
| `tests/test_role_permissions.py:999-1017` | 遍历 `app.routes` 的依赖树断言「每个写接口都有高于成员的权限」 | 这是**结构性**守护，不是逐条列举。它现在红了（因为 `system/templates` 是有意设计但没进白名单），但**设计是对的** |
| `db/session.py:105-131` | `_add_missing_columns` 的 docstring 明确写「只做 ADD COLUMN，唯一约束/类型变更必须写 Alembic」 | 把限制写在代码里，且配了 3 个补偿函数 |
| `db/session.py:363-481` | `_ensure_sqlite_product_key_scope` 重建表时**先探测再动手**、`PRAGMA foreign_key_check` 前后比对、未知列中止、`finally` 恢复 `foreign_keys=ON` | 这是「危险操作如何写」的正面教材 |
| 26 个迁移 | **downgrade 全部真实实现，无 `pass` 占位** | 比多数项目好；3 条降级会丢数据但在 `docs/migrations.md:121-127` 明确记录 |
| `migrations/_verify_baseline.py` | 逐表逐列比 `[type, notnull, default, pk]` + 逐索引比名字/unique/origin + 逐外键比 `ondelete`，且子进程隔离（因为 engine 是模块级单例） | **它已经是对的**，只需要搬进 CI |
| `app/src/main.ts` | `contextIsolation: true` / `nodeIntegration: false`、`setWindowOpenHandler` 一律 deny、`will-navigate` + `will-redirect` 双拦、`isTrustedPage()` 用 `path.resolve` 全等而非前缀 | 安全边界写对了 |
| `app/src/updateInstaller.ts:56-67` | `validateUpdateUrl` **精确匹配**域名，不用 `endsWith`/`includes` | `rules.md` 里「用 `endsWith` 会让 `aohuang888.xyz.evil.com` 通过」这条教训被真正落实了 |
| `scripts/verify-desktop-package.mjs` | 用 mock electron 在 VM 里真跑一遍 `dist/main.js` 断言 guard 行为 | 比「检查文件存在」强一个数量级 |
| `web/src/lib/queryKeys.ts` | 工厂函数形式，不是裸字符串 | 基础设施克制 |
| `web/src/main.tsx` | 明确写了「为什么不加全局 onError」 | 有意识的设计决策，不是遗漏 |
| `web/src/components/.../BatchPanel` | 定时器清理 | 比多数商业项目严谨 |
| `services/price_template.py:67-105` | 13 项费率 + 按站点区分配置 + 注释解释每个数字的来源 | 定价这种领域知识写进了代码而不是 wiki |

**这些地方在修复上述 31 条 P0 时不要顺手改。** 特别是 `_guarded` 和 `test_every_mutating_route_has_a_permission_above_member`——修 C 的 `system/templates` 白名单问题时要改的是**测试白名单**（把有意设计的两个端点加进 `DYNAMIC_WRITE_ROUTES`），不是给路由加权限（那会违背负责人 2026-11-23 的「学员也能改自己模板」要求）。

---

## 六、审计方法（可复现）

全部结论可用下列命令复现。**所有脚本都在 `mkdtemp` 临时库上跑，不触碰真实 `app.db`**（唯一例外是 F 报告里对生产服务器 `38.95.75.47` 的只读比对，已标注）。

```bash
# ── A 数据层 ──────────────────────────────────────────────
cd server
python migrations/_verify_baseline.py      # exit 1 → 有漂移（正确检出 short_id + schedules 默认值）
python scripts/ci_migration_check.py       # exit 0 → 但把漂移降级成 warning 放行
python -m ruff check .                     # 40 errors
python -m pytest tests -m unit -q          # 4 failed / 78
python -m pytest tests -q                  # 全量（含鉴权/多租户，不在 CI 范围）

# ── 凭据泄漏 ──────────────────────────────────────────────
cd .. && git ls-files | grep -E '^\.(captcha_|kw_|c[123]_|img|upload_)'
grep -o "x5secdata[^&]\{0,80\}" .captcha_dom.txt | head -3

# ── 版本校验 / 打包（E 报告的「已复核通过」项）──────────────
python -X utf8 scripts/check_version.py            # exit 0，四处均为 0.1.54
node scripts/verify-desktop-package.mjs            # exit 0
cd app && node --test src/*.test.mjs               # pass 29 / fail 0
```

### 关于「实证 vs 推断」的分级

F 报告第一版有一条 P0 结论被实测推翻（把「部署清单里没有」等同于「服务器上没有」），修正为 v2 并对每个 P0 标注了证据等级：
- `【已实测】` = 在真实生产服务器上跑命令验证过
- `【代码推断】` = 只读源码得出，未在运行态验证

**这个分级应该成为后续审计的标准做法。** 本总报告引用的 12 条最严重问题**全部标为已实测/已复现**；A / B / C / E 各报告的关键 P0 都由第二人独立跑过脚本复现。

---

## 七、一句话总结

> 这个项目的**基础设施 Discipline 明显高于平均水平**（`_guarded` 依赖树、`_verify_baseline` 逐列比对、`validateUpdateUrl` 精确匹配、26 个迁移全部有真实 downgrade），
> 但**业务层的「唯一权威」从未被划定** —— 同一个概念（售价、共享模式、模板选中、清单校验、安装包删除）反复出现第二份实现，于是 42% 的问题是补丁叠补丁，21% 是跷跷板。
>
> **修复重心不该是「再加一个判断」，而是每次修复前先回答：这件事的唯一权威是谁，其他实现什么时候删掉。**
