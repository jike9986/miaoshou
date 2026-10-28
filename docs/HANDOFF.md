# 交接文档（新对话从这里开始）

> 更新时间：2026-10-27，本轮修完审查报告全部 P0（7 项）与 P1（11 项）之后重写。
> 项目位置：`E:\codex工作台\p006\tk-desktop\`
> 数字口径：本文所有计数都附可复跑的命令，命令跑不出同样结果就说明文档过期了。
> 契约看 `docs/api.md`，实测状态看 `docs/status.md`，缺陷清单看 `docs/review-report.md`。

---

## 一、这是什么

「奥皇上品」的新一代桌面端，替代老的 `tk-product-automation`（0.1.4，稳定运行中，**不要动它**）。

技术栈：Electron 31 + React 18/TS + Tailwind + FastAPI + SQLAlchemy 2.0 + SQLite（WAL）。

---

## 二、当前状态（已修完审查报告 P0/P1，可运行）

### 后端（`server/`）

```bash
cd server && python -c "import main; print(len([r for r in main.app.routes if getattr(r,'path','').startswith('/api/v1')]))"
# → 78 个路由对象；方法+路径对 81 个
```

- 模块：auth / shops / collect-box / warehouse / groups / categories / images / publish / schedules / words / carrier / size-charts / titles / sync / system
- 两个后台 worker：图片任务回收（20s）、定时发布扫描（60s），异常**带堆栈落盘**（`logger.exception`），不再静默吞掉
- 能力：标题翻译、颜色规范化、违规词清理、标题清理预览、主图 9 张截断、发布三重保险（confirm / 总开关 / 语言校验）、跨店载体重建、定价模板、价格试算、尺寸表库、载体链接池、价格模板同步、货源链接回收、词库启停、主图 OCR、译文中文残留复查

### 前端（`web/`，8 个页面）

- 设计系统 17 个组件 + 4 个布局
- 页面：连接妙手 / 成品仓库 / 采集箱 / 发布记录 / 定时发布 / 违规词库 / 图片翻译 / 载体链接池
- 采集箱有「处理入仓」（单条 + 批量）；图片翻译页有「识别主图」（本地 OCR，不耗预算）
- 商品详情抽屉有「按词库清理」按钮（先预览清理结果填回输入框，再由用户点保存，不直接改库）
- 载体链接池页（`/carrier`）支持批量粘贴添加、单条删除、从仓库回收货源

### 桌面壳（`app/`）

- Electron 主进程启动后端 → **探测真实端口**（8812–8822）→ 用 `?apiBase=` 交给前端
- 打包：PyInstaller 后端 exe + electron-builder `win-unpacked` + Inno Setup 安装包

---

## 三、怎么验证（四条真命令）

```bash
# 1. 静态检查（Python 未定义名 / 语法；TS 类型）
python -m pyflakes server/api server/services server/core server/schemas server/main.py server/run_server.py
cd web && npx tsc --noEmit && cd ../app && npx tsc --noEmit && cd ..
# 预期：全部无输出

# 2. 构建
cd web && npx vite build && cd ..
# 预期：✓ built，产物 js ≈594 kB / gzip ≈183 kB（有 1 条 >500kB 警告，非错误）

# 3. P0/P1 回归（46 项，跑完自动清理，不动真实数据）
python scripts/regression.py
# 预期：合计 46/46 通过；基线 products=355 schedules=0

# 4. 真实后端进程冒烟（起 uvicorn 再打 18 条 GET）
python scripts/smoke_live.py
# 预期：16×200 + 2×502（categories / collect-box 因妙手不可达，UPSTREAM_ERROR，不是 500）
```

回归脚本 mock 了 `account_service.account_client`、`product_service.translated_info`、`publish_service.publish`，验的是**本服务逻辑**，不是妙手联通性。

其它验证脚本：`scripts/verify_process_e2e.py`（处理入仓端到端真写库）、`scripts/verify_schedule_e2e.py`、`scripts/verify_p0_4.py`。

---

## 四、本轮修掉的问题（审查报告 P0 7 项 / P1 11 项）

| 编号 | 问题 | 修法 |
|---|---|---|
| P0-1 | 采集箱 6 条路由全部 `NameError`（`credentials` 未声明） | 5 个 handler 签名补 `credentials: CredentialsHeader = None` |
| P0-2 | 采集箱服务用未导入的 `AppError`/`client_for`/`upstream_error` | 改用 `errors.validation` / `account_client` / `map_upstream_error` |
| P0-3 | 「处理入仓」后端有、前端零入口 | 采集箱页加单条 + 批量入口（`useProcessCollectItem`） |
| P0-4 | 定时任务 `int(list)` → 每轮抛错 → 同批发 3 次 | list-safe 计数 + 手动/自动共用同一 `execute()` |
| P0-5 | 载体重建部分失败时日志 detailId 张冠李戴 | `_pair_entries(..., saved_sources=...)` 按 `savedSources` 配对 |
| P0-6 | 未上架商品被标 `published`，永久不再选中 | `_mark_published` 只标 succeeded |
| P0-7 | 颜色规范化在 `build_product()` 之后，两字段不一致 | 提取 `_clean_color_props()` 在 build **之前**清洗 |
| P1-1 | 老版缺 15 条接口 | 已补价格试算 / 尺寸表 / 载体链接池 / 主图 OCR / 词库启停 / 标题预览+保存 / 价格模板同步 / 货源链接同步 / 译文中文残留复查；仍缺本地图片上传（无图床凭据）、软删除恢复、`image-plan`/`save-images`（见 `status.md` §5.1） |
| P1-2 | `claimOnly` 分支认领结果不校验失败 | 解析 `carrier_service.claimed_detail_ids()`；**上游回执为空时不再退化成全量**（旧写法 `parsed or detail_ids` 会把全失败报成全成功） |
| P1-3 | 站点语言校验永不触发（店铺无 `site`） | `_with_site_fallback` 回填 `site` + `siteKnown`，未知时返回显式 warning |
| P1-4 | `perRun` 语义与发布结果对不上 | 统一摘要 `requested/selected/published/failed/count/status(empty\|ok\|partial\|failed)/reason`；`failed` 取 `max(failures, selected-published)` 不相加 |
| P1-5 | worker 吞异常无告警 | `logger.exception` + 文件日志 `logs/server.log` |
| P1-6 | 多人各自 key 仅 3 条路由支持 | `CredentialsHeader` 覆盖 8 个模块 23 处 |
| P1-7 | 链接池为空直接 422、无 UI 补链接 | 新增 `/carrier/links` 三条路由 + `valid_source_link()` 校验 1688 域名 + 前端载体链接池页面（`/carrier`） |
| P1-8 | `mode=direct` 模板静默不生效 | 返回 `warning` 说明模板未生效 |
| P1-9 | `api.md` 与实现多处不符 | 逐条按代码校正（幂等未实现、限流只在图片、title 1–255、pageSize 21–100 直接 400 等） |
| P1-10 | `PATCH /schedules` 用 `exclude_none` 导致无法清空字段 | 改 `exclude_unset=True`，template 在 local 更新时可选 |
| P1-11 | 未连接妙手仍可点「提交翻译」 | 前端 `if (!auth?.connected)` 拦截 + 按钮 disabled |

---

## 五、环境限制（两个真卡点）

### 1. Electron 和 Tauri 都无法在这台机器创建窗口
- Electron：`app.whenReady()` 处 `0xC0000005` 崩溃（22/28/31 三个版本都一样）
- Tauri：`tauri::run` 处「拒绝访问 os error 5」
- 机器特征：3 个虚拟显示器驱动（向日葵 OrayIdd / GameViewer / ToDesk）+ AMD/NVIDIA 双显卡
- **解决**：换干净机器，或禁用那 3 个虚拟显示器适配器

### 2. 派生的进程不允许写 `%LOCALAPPDATA%` / `%TEMP%`，只能写工作区
打包后的 `aohuang-server.exe` 在默认数据目录下会 `PermissionError: [WinError 5]`；
同一路径 PowerShell 和 cmd 都能写 —— 是进程级写入范围限制，不是权限配置也不是代码问题。

**绕过方式**（已实测有效）：
```powershell
$env:AOHUANG_DATA_DIR = "E:\codex工作台\p006\_probe_data"   # 指到工作区
$env:TEMP = "E:\codex工作台\p006\_build_tmp"; $env:TMP = $env:TEMP  # 否则 NSIS 打不出安装包
```

**这台机器上已完成的打包验证**：
- `server/dist/server/aohuang-server.exe` 启动后 9 条接口全 200，`db:ok`
- `release/win-unpacked/resources/server/aohuang-server.exe`（安装包内那份）同样 9 条全 200
- `release/奥皇上品-0.1.0-Setup.exe` 113 MB，产物内含后端 exe 与 `windows_ocr.ps1`
- **没验证的**：Electron 窗口本身（限制 1）

另有一条与 Windows 版本相关：本地 OCR **只能用 `powershell.exe`（Windows PowerShell 5.1）**，PowerShell 7 调 WinRT 会报 `0x80131539`。

---

## 六、怎么跑起来

```powershell
# 后端
cd tk-desktop\server
python -m uvicorn main:app --port 8812
# 端口被占用时后端会自己往上找（8812–8822），日志里会打印实际端口

# 前端开发（默认代理到 8812；后端换端口时用 AOHUANG_API_PORT 覆盖）
cd tk-desktop
$env:AOHUANG_API_PORT="8813"; pnpm --filter web dev    # http://127.0.0.1:5173

# 完整打包（含静态检查关卡）
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

**token 位置**：`%LOCALAPPDATA%\aohuang-desktop\secret.key`
**数据库**：`%LOCALAPPDATA%\aohuang-desktop\app.db`
**当前数据**：355 商品 / 3 分组（精品 10、海外仓定制款 6、自营海外仓 3）/ 24 违规词 / 1 账号「罗蔓」（假凭据）/ 发布开关关闭

---

## 七、下一步（按优先级）

1. **换机器验证 Electron 窗口**（本机物理上无法创建窗口，改代码没用）
2. 补剩余老版接口：本地图片上传（需自建图床凭据）、软删除恢复、`image-plan`/`save-images`
3. 入仓后回写妙手草稿（`save_shop`）——当前入仓只写本地库
4. `facets()` / `stats()` 的全表扫描优化（目前每次列表请求扫 3 次）
5. 三处版本号（根 / `app` / `web` 的 `package.json`）加构建期一致性校验
6. `PATCH /titles/{ident}` 目前只改本地，需要同步回妙手时得接妙手的保存接口

---

## 八、坑位备忘（都是踩过的）

- `vite` 必须 `host: "127.0.0.1"`（默认只监听 IPv6，Electron 连不上）
- `vite` 的 `watch.ignored` 要排除 `shots/`（Chrome profile 会导致 EBUSY 崩溃）；
  截图目录现已统一为 `docs/screenshots/`，`web/vite.config.ts` 里两条都排除了
- `vite base: "./"`（`file://` 下绝对路径 404）
- 桌面端必须 HashRouter（`file://` 没有服务端路由）
- 图片必须 `referrerPolicy="no-referrer"`（alicdn 对 Referer 做 ACL，带 Referer 返 403）
- Electron 必须 `--no-sandbox`（这台机器不加会崩）
- 健康检查是 `/api/v1/system/health`，**别漏 `/v1`**（漏了会一直超时）
- `publish()` 的入参是**请求体形状的 dict**，不是散装参数
- 发布项要 `warehouseId`（数据库 id），不是 `warehouseKey`
- `schedule_service.as_dict()` 只含调度字段，不含 shopId/accountId/template
- FastAPI 路由顺序敏感：`POST /warehouse/price-preview` 必须排在 `GET /warehouse/{ident}` 之前，否则 `GET /warehouse/price-preview` 会掉进 `{ident}` 被当成商品标识返回误导性 404（现在用占位路由显式返回 405）
- `_clean_color_props` 在模块级调用 `product_service`，需在函数内局部 import，否则循环导入
- 本机把公网域名解析到 `198.18.0.0/15`（代理中转），做 SSRF 白名单时这个网段必须放行，否则所有图床都被拒
