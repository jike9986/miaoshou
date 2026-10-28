# 打包说明

## 产物
```
tk-desktop/release/
├─ 奥皇上品-0.1.0-安装程序.exe   108 MB   ← 交付给用户
└─ win-unpacked/                347 MB   ← 绿色版（可直接运行）
```

## 打包链路
```
web/  ──vite build──▶  web/dist（442KB JS + 42KB CSS，相对路径）
server/ ──PyInstaller──▶  server/dist/server（aohuang-server.exe 13MB + _internal 75MB）
app/  ──tsc──▶  app/dist（main.js + preload.js）
        └─Inno Setup─▶  奥皇上品-0.1.0-安装程序.exe
```

## 运行时结构
```
安装目录/
├─ 奥皇上品.exe              173MB  Electron 主程序
└─ resources/
   ├─ app.asar               16KB   主进程代码
   ├─ server/                88MB   后端（含 Python 运行时，用户无需装 Python）
   └─ web/                   622KB  前端静态文件
```

## 启动流程
1. Electron 启动 → spawn `resources/server/aohuang-server.exe`
2. 轮询 `http://127.0.0.1:8812/api/system/health` 直到就绪（最长 60s）
3. 读 `%LOCALAPPDATA%\aohuang-desktop\secret.key` 作为访问令牌
4. 通过同步 IPC 注入 `window.__DSH_BOOT__ = { apiBase, token, version, platform }`
5. `loadFile('resources/web/index.html')` 加载界面（file:// 协议）
6. 前端所有请求带 `Authorization: Bearer <token>` 打到 127.0.0.1:8812

## 已验证
| 项 | 方式 | 结果 |
|---|---|---|
| 后端 exe 独立运行 | 直接执行 + 健康检查 | ✅ `{"ok":true,"db":"ok"}` |
| 前端构建 | vite build | ✅ |
| 资源路径 | 检查 index.html | ✅ `./assets/...`（相对） |
| CORS file:// | OPTIONS 预检 | ✅ `access-control-allow-origin: file://` |
| 数据目录一致性 | 对比 run_server.py 与 main.ts | ✅ 都是 `%LOCALAPPDATA%\aohuang-desktop` |
| 安装包生成 | Inno Setup | ✅ 108MB |
| **安装后双击启动** | — | ❌ **本机无法验证**（Electron 窗口崩溃，见 env-issues.md） |

## 打包命令
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```
或分步：
```powershell
pnpm build:web
cd server && python -m PyInstaller --noconfirm server.spec
cd .. && pnpm build:app
"/c/Program Files (x86)/Inno Setup 6/ISCC.exe" installer/setup.iss
```
