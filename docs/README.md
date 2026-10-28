# 奥皇上品 · 桌面端

> 完整的开发/构建/部署说明见仓库根 [README.md](../README.md)，
> 分支与提交规范见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## 一键启动（开发）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
```

会依次启动：FastAPI (8812) → Vite (5173) → Electron 窗口。

## 单独启动

```powershell
# 后端
cd server
python -m uvicorn main:app --reload --port 8812

# 前端（浏览器预览）
pnpm --filter web dev

# Electron
cd app
pnpm dev
```

## 打包

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

产物在 `release/`。

## 目录

```
tk-desktop/
├─ app/     Electron 主进程（窗口、IPC、自动更新）
├─ web/     React 前端（设计系统 + 业务页面）
├─ server/  FastAPI 后端（API-first，含 migrations/ 迁移）
├─ docs/    架构 / API / 迁移 / 截图
└─ scripts/ 开发与打包脚本
```

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [architecture.md](architecture.md) | 分层架构、数据模型、设计决策 |
| [api.md](api.md) | 全部 HTTP 接口 |
| [migrations.md](migrations.md) | Alembic 用法与迁移分工 |
| [design-system.md](design-system.md) | 设计令牌与组件规范 |
| [screenshots/](screenshots/README.md) | 截图索引（35 张） |
| [HANDOFF.md](HANDOFF.md) | 交接备忘（踩过的坑） |
| [status.md](status.md) | 当前进度与证据说明 |
| [parity-checklist.md](parity-checklist.md) | 与旧版功能对照 |
| [review-report.md](review-report.md) | 代码评审报告 |
| [package-notes.md](package-notes.md) | 打包相关备忘 |
| [env-issues.md](env-issues.md) | 环境问题记录 |

## 已知环境适配

- Windows 上 Electron 沙箱会导致 `EXCEPTION_BREAKPOINT`，主进程已强制 `--no-sandbox`。
- Electron 二进制通过 npmmirror 手动安装（`app/node_modules/electron/dist`）。
