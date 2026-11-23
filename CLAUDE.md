# 奥皇上品 · AI 助手必读规范

> 每次开新对话，从这里开始读。详细内容分散在下列文档，遇到相关任务先查对应文档再动手。

---

## 项目概览

| 项目 | 值 |
|---|---|
| 名称 | 奥皇上品桌面端（miaoshou） |
| 工作目录 | `D:\缓存文件夹\codex工作台\p006\miaoshou` |
| 技术栈 | Electron 31 + React 18/TS + Tailwind + FastAPI + PostgreSQL |
| 生产服务器 | `38.95.75.47`，服务名 `aohuang-listing-v2` |
| 桌面安装目录 | `D:\软件\奥皇上品` |
| SSH 密码文件 | `C:\Users\Administrator\Desktop\雨云.txt`（最后一行） |

---

## 文档更新规则（每次对话必须执行）

**用户说的所有有用信息，当次对话里就要写进文档。不只是"规范"——任何业务背景、偏好、限制、临时决定、发现的问题都算。对话结束前检查一遍有没有漏。**

| 用户说的内容 | 更新哪里 |
|---|---|
| 新的 bug 或已修复的问题 | `docs/features/{功能}/tasks.md` |
| 新的业务规则、约束、偏好 | `docs/project/rules.md` + 对应功能的 `spec.md` |
| 新的需求或验收标准变更 | `docs/features/{功能}/requirements.md` |
| 部署/环境相关信息 | `CLAUDE.md` 和/或 `DEPLOY.md` |
| 技术方案变更 | `docs/features/{功能}/spec.md` |
| 临时决定、背景说明、特殊情况 | `docs/features/{功能}/spec.md` 或 `docs/project/rules.md` |

> 不更新 = 下次对话丢失，等于白说。

---

## 必须遵守的规则（不可绕过）

### 1. 版本号必须四处同步

每次发版，必须同时更新以下 4 个文件，版本号保持完全一致：

```
package.json                   # 根
web/package.json               # 前端
app/package.json               # Electron（electron-builder 读这个出安装包名）
server/core/constants.py       # 后端（APP_VERSION 兜底，优先读根 package.json）
```

> 2026-11-23 改：安装包从 Inno Setup 换成 electron-builder 自带的 NSIS，
> `installer/setup.iss` 已删除，清单从五处变四处。原因见
> `docs/project/auto-update-nsis-2026-11-23.md`。

一条命令核实（exit 0 才算过）：

```bash
python -X utf8 scripts/check_version.py
```

> 2026-11-18 补（已随 Inno 一起作废）：`installer/setup.iss` 曾漏在同步清单外，
> 导致 0.1.25 时它停在 0.1.23、`scripts/build.ps1` 第一步直接 throw。

### 1.1 打包事实（别把正常设计当 bug）

- 正式客户端**只连远程** `https://aohuang888.com/miaoshou-platform`，`app.isPackaged` 时 `useRemoteServer()` 恒 true。
- `app/electron-builder.yml` 的 `extraResources` 只放 `../web/dist → web`，**不分发本地后端**。`release/win-unpacked/resources/` 没有 `server/` 是正常的。
- 只替换 `resources/web/` ≠ 打完包。`app.asar` 里的版本会留在旧值，`node scripts/verify-desktop-package.mjs` 会报 `Packaged app version is stale`。要出可发包必须重跑 `scripts/build.ps1` 或 `cd app && pnpm exec electron-builder --win --dir`。

### 2. 部署流程（不要手动 scp）

**后端部署**（上传 Python 文件 + 重启服务）：
```bash
cd /d/缓存文件夹/codex工作台/p006/miaoshou
python scripts/deploy_server.py
```

**桌面端部署**（先构建，再复制到安装目录）：
```bash
# 在 bash（Git Bash）中运行，不要用 PowerShell
cd /d/缓存文件夹/codex工作台/p006/miaoshou
pnpm build
cmd /c scripts\\do_deploy.bat
```

### 3. Shell 约束

- 不要用 PowerShell 运行 `npx` / `pnpm`（执行策略限制）
- 用 bash（execute_bash 工具）运行所有构建命令
- `cmd /c` 可以运行 `.bat` 文件

### 4. draft_verified_revision 规则

编辑商品的任何字段（图片、重量、尺寸、分类、标题）后，必须同步 `draft_verified_revision`：

```python
if product.automation_result and product.draft_verified_revision is not None:
    product.draft_verified_revision = int(product.revision or 0)
```

否则该商品会从自动化结果页消失。

### 5. revision_guard 规则

任何修改商品的操作必须调用 `revision_guard()` 做乐观并发控制，不允许直接写 `product.revision`。

---

## 文档导航

| 文档 | 内容 |
|---|---|
| [docs/project/rules.md](docs/project/rules.md) | 完整约束与代码规范 |
| [docs/project/deploy.md](docs/project/deploy.md) | 部署手册（服务器+客户端） |
| [docs/features/automation/](docs/features/automation/) | 自动化处理功能 |
| [docs/features/warehouse/](docs/features/warehouse/) | 成品仓库功能 |
| [docs/features/collect-box/](docs/features/collect-box/) | 采集箱功能 |
| [docs/features/publish/](docs/features/publish/) | 发布记录功能 |
| [docs/architecture.md](docs/architecture.md) | 系统架构（进程/分层/数据库） |
| [DEPLOY.md](DEPLOY.md) | 运维手册（nginx/证书/环境变量） |

---

## 快速诊断

### 商品在自动化结果里消失
→ `draft_verified_revision != revision`，查 warehouse_service.py / warehouse.py 里所有修改商品的位置，补同步语句。

### 批次进度卡在 50%
→ `imagePercent` 在 `imagePlanned=0` 时返回 0，导致 `(100+0)/2=50`。检查 ProgressPanel.tsx。

### SKU 显示乱码（hex ID）
→ skuMap key 是按 attrValueId 升序排列的，不按维度顺序。用全局 `flatIdToLabel` Map 查找。

### 页面滚动放大缩小
→ Electron 默认响应 Ctrl+Scroll。main.tsx 已全局拦截，确认 `e.ctrlKey` 时 `e.preventDefault()`。

### SSH 连不上服务器
→ 先用 Python socket 测试端口 22 是否有 SSH banner，有时需要等几分钟恢复。
