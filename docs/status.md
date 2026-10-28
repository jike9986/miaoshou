# 项目状态

> 本文档数字由 2026-10-27 审查后实测生成（本机 Windows，`E:\codex工作台\p006\tk-desktop`）。
> **每条数字都附取证命令**，命令跑不出同样结果就说明文档过期了。
> 只看不做验证的结论已全部删除 —— 本文件不引用任何未经本次复验的测试计数或截图。
> 最新交接请看 `docs/HANDOFF.md`；契约看 `docs/api.md`；问题清单看 `docs/review-report.md`。

## 一、口径说明（先看这个）

- **「实测」**＝我在本机起后端进程（`python -m uvicorn main:app --port 8825`，后期用 8826 复验新路由，数据目录仍是真实 `%LOCALAPPDATA%\aohuang-desktop`）+ `curl` 打的读写请求，输出原文贴在下面对应位置。外部上游（妙手）不可达，所以**凡是依赖上游的调用都只能验到「路由通、参数校验对、错误码对」**，验不到成功路径。
- **「静态」**＝读代码 / grep / 查 SQLite，不给运行时结论。
- 后端在 2026-10-27 这天被多个 agent 并行修改，**接口数、行号随时会变**。看到不一致时以命令输出为准，别以本文档为准。

## 二、真实完成度（经独立审查修正）

| 层 | 状态 | 取证 |
|---|---|---|
| 后端接口 | 78 个路由对象 / 81 个方法+路径对（快照 2026-10-27）；采集箱不再抛 `NameError` | 见 §三 命令 ① ②；`GET /collect-box?pageSize=20` → 502 `UPSTREAM_ERROR`（不再是 500） |
| 后端静态检查 | **通过**：`pyflakes` 无输出，exit=0 | `python -m pyflakes server/api server/services server/core server/main.py`（见 §三之二） |
| 前端类型检查 | **通过**：`tsc --noEmit` 无输出，exit=0 | `cd web && npx tsc --noEmit; echo "exit=$?"` |
| 前端构建 | **通过**：`vite build` ✓ built in 2.62s；`index-BiqOanaj.js 593.68 kB (gzip 182.67)`、`index-coF7kz6J.css 42.91 kB (gzip 8.75)`；有 1 条 `>500 kB chunk` 警告（非错误，要时可 code-split） | `cd web && npx vite build` |
| 接口冒烟（真实进程） | 18 条 GET：16 条 200，2 条 502（`categories`、`collect-box`，**上游妙手不可达**，不是后端 500） | `python scripts/smoke_live.py` |
| P0/P1 回归 | **29/29 通过**，跑完自动清理，基线 `products=355 schedules=0` | `python scripts/regression.py` |
| 打包 | 已有 `release/` 产物，**本次未重新打包、未复验安装** | — |
| 功能对齐 | 老版缺口已补：价格预览、尺寸表、载体链接池、词库启停、OCR 主图识别、标题预览/保存、价格模板同步、货源链接同步、译文中文残留复查；仍未补：本地图片上传、软删除恢复、软件内更新下载、`image-plan`/`save-images` | 见 §五 |
| Electron 窗口 | 未复验（`app/src/main.ts` 关沙箱 + `no-sandbox`） | — |
| 数据 | 355 商品 / 3 分组 / 24 违规词 / 1 账号（假凭据）/ 0 定时任务 / 0 发布记录 / 发布开关关闭 | 见 §六 |

## 三、接口总数（附命令与输出）

```bash
# ① 路由对象数（一个对象可承载多个 HTTP 方法）
cd server && python -c "import main; print(len([r for r in main.app.routes if getattr(r,'path','').startswith('/api/v1')]))"
# 输出：78

# ② 方法+路径对数（「接口数」用这个更准）
cd server && python -c "import main; rs=[r for r in main.app.routes if getattr(r,'path','').startswith('/api/v1')]; print(sum(len(r.methods-{'HEAD'}) for r in rs))"
# 输出：81

# ③ 全 app 路由（含 GET / 与 GET /api/system/health 两条非 /api/v1 路径）
cd server && python -c "import main; print(len(main.app.routes))"
# 输出：76

# ④ 源码装饰器计数（含未挂载的，交叉核对用）
cd server && grep -hcE "^@(router|alias_router|health_router)\.(get|post|patch|put|delete)" api/*.py | awk '{s+=$1} END {print s}'
# 输出：73
```

- ① 与 ② 差 3：`GET/PUT/PATCH/DELETE /warehouse/price-preview` 是**一个路由对象带四个方法**（占位，非 POST 返回 405）。
- ④ 比 ② 少 4：那条占位路由写成 `@router.api_route(..., methods=[...])`，不在 grep 的动词表里（少算 1 条装饰器），但贡献 4 个方法+路径对。
- **分模块（方法+路径）**：auth 6 / shops 3 / collect-box 6 / warehouse 12 / groups 4 / categories 3 / images 8 / publish 5 / schedules 5 / violation-words 5 / words 2 / carrier 3 / size-charts 6 / system 9 = **77**。明细见 `docs/api.md` 的「接口索引」。
- 这个数字在 2026-10-27 当天被并行开发改过至少 4 次（61 → 64 → 73 → 74）。**要引用前先跑一遍 ②**。

## 三之二、测试与回归（可复跑，别信口头数字）

四条命令，跑不出同样输出就说明代码变了：

```bash
# 1) 后端静态检查（未定义名、未使用导入）
python -m pyflakes server/api server/services server/core server/main.py
# 无输出，exit=0

# 2) 真实进程冒烟：脚本自己起 uvicorn（8891）→ 打全部只读 GET → 关进程
python scripts/smoke_live.py
# 200 system/health, system/version, auth/status, shops, warehouse?pageSize=5,
#     warehouse/stats, groups, schedules, publish/logs?pageSize=5,
#     violation-words?pageSize=5, violation-words/counts, carrier/links,
#     size-charts, size-charts/resolve?site=MY, images/jobs?pageSize=5,
#     images/limits, images/usage            → 一共 16 条 200
# 502 categories, collect-box?pageSize=5     → 上游妙手不可达（UPSTREAM_ERROR），不是崩溃
# 说明：这两个 502 属于「上游不可达」，与后端 500 是两件事；报告里的 5xx 要分开看。

# 3) P0/P1 回归（mock 上游，验的是本服务逻辑）
python scripts/regression.py
# 29/29 通过；覆盖 P0-1/2/3/4/5/6/7、P1-1/4/7/10；跑完清理测试数据
# 基线：products=355 schedules=0

# 4) 前端
cd web && npx tsc --noEmit; echo "exit=$?"   # 无输出，exit=0
cd web && npx vite build                     # ✓ built in 2.62s
```

**`scripts/regression.py` 的能力边界**：它 mock 了上游（`account_service.account_client`、`product_service.translated_info`、`publish_service.publish`），所以验证的是**本服务的逻辑**（幂等点、事务、字段一致性、四态判定），**不是妙手联通性**。妙手能不能通，看 `smoke_live.py` 里那两条 502 —— 上游没凭据就是通不了。

覆盖到的关键结论（本次实跑输出）：

- `P0-3 save=false 只预览` → `HTTP 200, 355->355`；`P0-3 save=true 真写库` → `HTTP 200, 355->356`（测完删除回 355）。
- `P0-7 两处颜色一致` → `['Beige'] == ['Beige']`，原始 `Y18[beige]` 已规范化。
- `P1-4 状态机` → `empty / ok / partial / failed` 四态全部命中；摘要 `{'requested': 2, 'selected': 2, 'published': 1, 'failed': 1, 'status': 'partial'}`，`failed` 不重复计数。
- `P0-4 一轮只调一次 publish` → `1 次`（旧 bug 会 3 次）。
- `P1-10 显式 null 清空` → `HTTP 200, cleared=True`。
- `P1-1` → 尺寸表 200、价格预览拒绝空 items（400）、非 POST 405、图片识别拒绝非法来源（400）。

## 四、版本号（三处，当前一致，但靠人工同步）

```bash
cd E:/codex工作台/p006/tk-desktop && grep -m1 '"version"' package.json app/package.json web/package.json
# package.json:      "version": "0.1.0",
# app/package.json:  "version": "0.1.0",
# web/package.json:  "version": "0.1.0",
```

- 后端 `APP_VERSION` **不再写死**：`server/core/constants.py:9-30` 的 `_package_version()` 会向上找仓库根 `package.json` 读 `version`（找不到回退 `"0.1.0"`）。运行期实测 `GET /api/v1/system/health` → `"version":"0.1.0"`。
- **仍是待办**：三处版本号是三个独立副本，没有构建期校验/注入。改版本要手工同步三处，漏一处不会报错。`app/package.json` 决定安装包版本，根 `package.json` 决定后端自报版本 —— 两者不一致时 `/system/version` 的 `updateAvailable` 判断会失真。

## 五、已知未修复 / 未实现（逐条附命令）

跑这些命令，输出 0 就说明「确实没有」；输出非 0 说明已经补上了，请顺手更新本文档。

### 5.1 仍未实现

| 项 | 状态 | 取证命令与当前输出 |
|---|---|---|
| 本地图片上传（老版 `image_upload.py` + 自有图床） | **没有**。`/images/translate` 只接受 `url` 或 `imageBase64`，本地文件得自己找地方放 | `grep -rn "upload" server/api server/services --include=*.py` → 只命中 `api/size_charts.py:1` 的文档注释 |
| 软删除恢复接口（`POST /warehouse/{id}/restore` 之类） | **没有**。误删只能改库（`UPDATE products SET deleted_at=NULL, status='ready'`） | `grep -rn "restore" server --include=*.py` → 0 命中 |
| 软件内下载 / 静默安装更新 | **未闭环**。`POST /system/update` 只校验 URL+sha256 并拼一个**不存在**的文件路径；Electron 侧没有 update IPC；前端没有调用方 | `grep -n "update" app/src/main.ts app/src/preload.ts` → 0 命中；`grep -rn "system/update" web/src` → 0 命中；`grep -rn "DIGEST_MISMATCH" server --include=*.py` → 只在 `core/errors.py:44` 定义，零引用 |
| 入仓后回写妙手草稿（老版 `product_workflow.py` 的 `save_shop`） | **没有**。`save_from_collect_box()` 只写本地库 | `grep -n "save_shop" server/services/warehouse_service.py server/api/collect_box.py` → 0 命中（`save_shop` 只被 `carrier_service.py:365` 用于载体重建） |
| `Idempotency-Key` 幂等 | **没有**。重复请求会重复执行 | `grep -rn "Idempotency" server/api server/core server/services` → 0 命中；实测同一 key 连打两次 `POST /images/translate`，两次都真实执行、两个不同 `requestId` |
| 全局请求限流（req/s） | **没有**。429 只属于图片配额 | `grep -rn "status=429" server` → 只有 `image_service.py` 一处 |
| 老版其余接口：`image-plan` / `save-images` / `title-words` | **没有对应路由**（`save-title`、`price-sync`、`sync-source-links` 已补，见 5.2） | `for k in image-plan save-images title-words; do grep -rniE "$k" server/api server/services --include=*.py \| wc -l; done` → 全 0 |

### 5.2 已补上（本次实测过；从「缺失」清单移出）

| 项 | 现状 | 取证 |
|---|---|---|
| 价格预览（老版 `/api/price-preview`） | `POST /api/v1/warehouse/price-preview`，不写库不调上游，算法与真实发布同源 | 实测 `{"items":[{"warehouseId":"355"}],"template":"默认模板"}` → 200，`priceBefore 10.41–12.86 → priceAfter 36.63–41.25 MYR` |
| 尺寸表（老版三件套） | `/size-charts` 6 条（list / resolve / detail / create / patch / delete） | 实测 `GET /api/v1/size-charts` → 200 `{"items":[],"charts":[],"total":0,"sites":[]}` |
| 载体链接池（老版无，F5 的缺口） | `/carrier/links` GET/POST/DELETE + **前端页面** `web/src/features/carrier/CarrierPage.tsx`（路由 `/carrier`，侧栏「系统 → 载体链接池」）；页面含手工粘贴批量添加、单条删除、从仓库回收 | 实测 `GET /api/v1/carrier/links` → 200 `{"total":0,"usable":0,"items":[],"limit":200}`；加入 `detail.1688.com/offer/999888777.html` → `added:1`，删除 → `removed:1`、`total:0`（不留残留）；非 1688 域名 → `added:0` 并进 `invalid` |
| 词库启停（H4） | `PATCH /violation-words/enabled`，只翻 `enabled` 位不删词 | 实测 `{"enabled":false,"words":["zzz-not-exist"]}` → 200 `{"updated":0,"missing":["zzz-not-exist"],...}` |
| OCR 主图文字识别（老版 `analyze-images`） | `POST /images/analyze`，**已补且实测可用**：走 `powershell.exe`（Windows PowerShell 5.1；**PS7 不支持 WinRT**）执行 `windows_ocr.ps1`；来源白名单 alicdn/chengji-inc/taobao/tmall + 私有地址拒绝（防 SSRF）；结果按 URL sha256 缓存在 `%LOCALAPPDATA%/aohuang-desktop/images/ocr/`；不消耗翻译预算 | 实测真实商品主图（`earth-rt.chengji-inc.com`）→ 200 `action:"review"`、`error:false`（该图 OCR 没识别到文字）；`example.com` / `localhost` → 200 但 `error:true`（白名单拒绝）；空数组 → 400。前端按钮在 `web/src/features/images/ImagesPage.tsx` → `useAnalyzeImages()` |
| 采集箱接口 500（P0-1 `credentials` NameError） | 已修，6 条路由都带 `credentials` 形参 | 实测 `GET /api/v1/collect-box?pageSize=20` → 502 `UPSTREAM_ERROR`（走到上游了，不再是 500） |
| 前端「处理入仓」入口（P0-3） | 已接入：`useProcessCollectItem()` + 采集箱页单条/批量表单 | `grep -rn "collect-box/\${.*}/process" web/src` → `web/src/lib/queries.ts:459` |
| 标题清理预览（老版 `/api/preview`） | `POST /titles/preview`，复用 `word_service.matcher()` + `clean_title()`，`review` 类词只记录不删 | 实测 `{"titles":["好鞋 包邮 秒杀"]}` → 200 `removedWords:["包邮","秒杀"]`、`changed:true`；空 body → 400 |
| 单标题保存（老版 `/api/save-title`） | `PATCH /titles/{ident}`，ident 支持数据库 id 与 `{detailId}:{shopId}`；**只改本地，不回写妙手** | 实测改真实商品标题 → 200 且库里 `title`/`payload.title` 都变了，随后恢复原值；商品数仍 355、无残留验证标题 |
| 价格模板同步（老版 `/api/price-sync`） | `POST /sync/price-templates`，调 `miaoshou.price_templates()`，只读不落库 | 实测本机假凭据 → 502 `UPSTREAM_ERROR`（上游不可达，属预期）；`pageSize=50` → 400 `VALIDATION_ERROR`（上游上限 20） |
| 货源链接同步（老版 `/api/sync-source-links`） | `POST /sync/source-links`，复用 `harvest_from_payloads()`，只收 1688 链接 | 实测 `{"all":true,"limit":3}` → 200 `{"scanned":3,"added":0,"total":0,"usable":0}`（现有商品 payload 里没有 1688 货源字段）；空 body → 400 |
| 译文中文残留复查（老版 `image_review`） | 图片任务变 `ready` 时对本地结果图跑一次本地 OCR，检出中文只写进 `job.message` 提示，**不改状态不阻断**（图已扣费，判失败会让操作者重复付费） | `image_service._chinese_residue()`；实测空白 PNG → `check_local_file` 返回 `error:false`、`hasChinese` 不为 true（不误报）；结果图缺失 → `error:true` 且不抛异常 |
| 图片 OCR 的前端入口 | `web/src/features/images/ImagesPage.tsx` 的「识别主图」按钮 + 结果面板 | `useAnalyzeImages()` → `POST /images/analyze`；按钮不依赖妙手连接（本地跑，不耗翻译预算） |

### 5.3 已知不一致（能用，但语义打架，别照文档猜）

| 项 | 表现 | 取证 |
|---|---|---|
| ~~`POST /violation-words` 的 `enable`/`disable`~~ | **已修**：`WordAction` 放开为 7 值，服务层分支可达 | 实测 `{"action":"disable","word":"<已有词>"}` → 200 `{"updated":1,"enabled":false}`，库里 `enabled=False` 且词未被删；`enable` 能恢复；非法 action 仍 400。回归脚本 `scripts/regression.py` 的 P2-10 三例均通过 |
| ~~端口递增没人用~~ | **已修**：Electron 启动后探测 `8812–8822` 的真实健康端口，再通过 `?apiBase=` 交给前端；Vite 代理可用 `AOHUANG_API_PORT` 覆盖 | `app/src/main.ts` 的 `detectApiPort()` + `apiPort`；顺带修掉健康检查路径漏 `/v1`（原为 `/api/system/health`，永远探不到）。实测占住 8812 后 `pick_port()` → 8813，真实 uvicorn 在 8812 时 `/api/v1/system/health` → 200 |
| `refresh=false` | `/system/version` 的 `refresh` 参数两分支调的是同一个函数，**不缓存** | `server/services/system_service.py:87` |
| `sort=savedDesc`（采集箱） | 按「是否已入仓」排序，不是按时间（参数名歧义，实现本身正确） | `server/services/collect_box_service.py:152-153` |
| 更新清单文案 | 清单 `notes` 写「自动下载、静默安装、自动重开」，但该能力不存在（见 5.1） | 实测 `GET /api/v1/system/version` 返回的 `notes` 原文 |

## 六、数据状态（本次直查 SQLite）

```bash
python - <<'PY'
import os, sqlite3, pathlib
base = os.environ.get('LOCALAPPDATA') or str(pathlib.Path.home())
c = sqlite3.connect(str(pathlib.Path(base)/'aohuang-desktop'/'app.db'))
for q in ('select count(*) from products where deleted_at is null',
          'select count(*) from groups',
          'select count(*) from violation_words',
          'select count(*) from accounts',
          'select count(*) from schedules',
          'select count(*) from publish_logs'):
    print(q, '->', c.execute(q).fetchone()[0])
PY
# products(未删) -> 355
# groups          -> 3
# violation_words -> 24
# accounts        -> 1
# schedules       -> 0
# publish_logs    -> 0
```

- 库路径：`%LOCALAPPDATA%\aohuang-desktop\app.db`（6.3 MB）。
- 设置项（`settings` 表）：`security.readOnly=false`、`publish.enabled=false`（发布总开关关闭）、`image.limits={"minute":600,"hour":6000,"day":60000,"budgetMilliyuan":10000}`。
- 账号只有 1 个（`罗蔓`，`appIdHint`/`secretHint` 均为空串 —— 历史导入的账号没存 hint），妙手凭据是假的，所以所有上游调用都是 `502 UPSTREAM_ERROR ConnectError`。
- 商品 `id=355` 的 `title` 当前是 `"A"`（有人在做标题长度边界测试留下的）。这是**真实数据被改过**的证据，不代表正常状态。

## 七、本次亲手跑过、可以复现的接口（其余不要信）

全部在本机 `127.0.0.1:8825` / `:8826`（同一份真实数据库，只读或可回滚的写）上执行，`Authorization: Bearer $(cat %LOCALAPPDATA%\aohuang-desktop\secret.key)`。

| 请求 | 实测结果 |
|---|---|
| `GET /api/v1/system/health`（无 token） | 200 `{"ok":true,"name":"aohuang-listing-server","version":"0.1.0","db":"ok",...}` |
| `GET /api/v1/auth/status` | 200，`accounts[0]` 含 `secretHint`；顶层含 `serverName`；`serverVersion":"0.1.0"` |
| `GET /api/v1/system/version` | 200，`current":"0.1.0"`、`latest":"0.1.4"`、`updateAvailable":true`、`sizeBytes":null` |
| `GET /api/v1/collect-box?pageSize=50` | 400 `VALIDATION_ERROR`「每页数量必须在 1 到 20 之间」 |
| `GET /api/v1/collect-box?pageSize=20` | 502 `UPSTREAM_ERROR ConnectError`（路由与参数都通，卡在上游） |
| `POST /api/v1/collect-box/123/process` body `{}` | 400 `VALIDATION_ERROR`「参数校验失败：shopId Field required」 |
| `POST /api/v1/collect-box/3482714692/process` body `{"shopId":"18077147","save":false}` | 502 `UPSTREAM_ERROR ConnectError`（未入仓，`save=false` 即使成功也会回滚） |
| `POST /api/v1/warehouse/price-preview` body `{"items":[{"warehouseId":"355"}],"template":"默认模板"}` | 200，`repriced":1`，`priceBefore 10.41–12.86` → `priceAfter 36.63–41.25` (MYR)，`rate 1.633...`、`rateSource":"product"` |
| `GET /api/v1/warehouse/price-preview` | 405 `VALIDATION_ERROR`「价格试算请用 POST …」 |
| `GET /api/v1/size-charts` | 200 `{"items":[],"charts":[],"total":0,"sites":[]}` |
| `GET /api/v1/carrier/links` | 200 `{"total":0,"usable":0,"items":[],"limit":200}` |
| `PATCH /api/v1/violation-words/enabled` body `{"enabled":false,"words":["zzz-not-exist"]}` | 200 `{"updated":0,"missing":["zzz-not-exist"],"enabled":false,...}` |
| `POST /api/v1/violation-words` body `{"action":"disable","word":"x"}` | 400 `VALIDATION_ERROR`（action 枚举里没有 disable） |
| `POST /api/v1/system/update` body `{"url":"https://evil.example.com/x.exe","sha256":"a"*64}` | 403 `FORBIDDEN_ORIGIN`，`details.host="evil.example.com"` |
| `GET /api/v1/system/version` 带 `Origin: https://evil.example.com` | 200，响应头里**没有** `access-control-allow-origin`（CORS 不返回 403） |
| `POST /api/v1/images/translate`（同一 `Idempotency-Key` 连发两次） | 两次都 503 `IMAGE_KEY_MISSING`，`requestId` 不同（无幂等、无密钥） |
| `GET /api/v1/images/limits` | 200 `{"limits":{"minute":600,"hour":6000,"day":60000,"budgetMilliyuan":10000},...}` |
| `POST /api/v1/images/analyze` body `{"images":[]}` | 400 `VALIDATION_ERROR`「images 必须是非空数组」 |
| `POST /api/v1/images/analyze` body `{"images":["https://example.com/a.jpg"]}` | 200，单条 `action:"review"`、`error:true`（白名单外，逐张失败不拖垮整批） |
| `POST /api/v1/images/analyze` body `{"images":["https://localhost/a.jpg"]}` | 200，`error:true`（私有/回环地址被拒，防 SSRF） |
| `POST /api/v1/images/analyze` body 一张真实主图 `earth-rt.chengji-inc.com/...png` | 200，`action:"review"`、`error:false`、`text:""`（本地 OCR 执行成功，该图没识别到文字） |
| `GET /api/v1/violation-words/counts` | 200 `{"remove":24,"review":0,"limit":500,"remaining":476}` |

## 八、待修（见 review-report.md）

- 审查报告列的是 P0 × 7 / P1 × 11 / P2 × 10（2026-10-27 版本）。**P0/P1 的修复有脚本可复跑**：`python scripts/regression.py` → 29/29 通过（覆盖 P0-1..7 与 P1-1/4/7/10，mock 上游）。P2 的 10 项**没有**可复跑脚本，本文档不对它们下结论。
- 当前最该处理的是 §五 的 5.1 与 5.3：能力缺口（图片上传 / 软删除恢复 / 更新下载 / 入仓回写妙手）与语义不一致（enable-disable、端口、refresh、savedDesc）。

## 九、不要拿这个当证据的东西

- `.smoke/report.json` 的 `report` 段：7 条记录（warehouse / collect / schedules / words / publish / images / unlock）的 `dom.url` **全部是 `/unlock`**，`cards`/`rows` 都是 0 —— 即页面被守卫重定向到解锁页，业务页一个都没测到。复验：

  ```bash
  python -c "import json;d=json.load(open('.smoke/report.json',encoding='utf-8'));print([(r['name'],r['dom']['url']) for r in d['report']])"
  # [('warehouse','/unlock'), ('collect','/unlock'), ('schedules','/unlock'), ('words','/unlock'), ('publish','/unlock'), ('images','/unlock'), ('unlock','/unlock')]
  ```

  同文件 `flow` 段有 1 条历史交互记录（`state.kpi = ['355','0','0','0']`、`cards: 24`），但那是别人跑的、没有本次复验，**不作为渲染证据**。
- 任何「N 项冒烟测试通过」「7 个页面全部渲染」之类的计数：本文档不写，因为没有可复现的测试脚本输出。
- `docs/screenshots/` 里的截图（`page-*.png`、`preview-*.png`、`shots/` 原目录下的
  `f-*.png` 等，共 35 张）：不做证据引用。
