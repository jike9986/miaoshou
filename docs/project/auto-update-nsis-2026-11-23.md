# 自动升级链路（2026-11-23 重做，安装包改用 electron-builder NSIS）

## 一句话结论

**关掉软件 → 静默装新版 → 自动重启 → 桌面快捷方式指向新版本**，全链路已实测通过（0.1.41 → 0.1.42，`setup exit=0`，进程自动恢复，`updates/` 自动清空）。

---

## 为什么换掉 Inno Setup

旧安装包用 Inno Setup，客户端按 NSIS 的约定传 `/S` 求静默安装。Inno **不认识 `/S`**，直接弹出图形安装向导（实测窗口标题「安装 奥皇上品 0.1.34」）。用户看到的就是：

> 点击还是自动更新然后关闭又不重启，打开又安装

Inno 的静默参数是 `/VERYSILENT /SUPPRESSMSGBOXES /NOCANCEL /NORESTART /CLOSEAPPLICATIONS /DIR=`，与 NSIS 完全不同。既然目标形态就是「静默装 + 自动重启 + 快捷方式始终最新」，直接换成 electron-builder 自带的 NSIS —— 大厂 Electron 应用（VS Code、Slack、Discord）都是这条路，参数少、行为确定。

### 现在的安装包配置

`app/electron-builder.yml`：

```yaml
nsis:
  oneClick: false              # 允许用户选目录
  perMachine: false            # 装到当前用户，不弹 UAC
  allowToChangeInstallationDirectory: true
  createDesktopShortcut: false # 快捷方式由 installer.nsh 自己建（见下）
  createStartMenuShortcut: false
  artifactName: 奥皇上品-${version}-Setup.${ext}
  executableName: AohuangShangpin
  include: build/installer.nsh
```

### 客户端传的参数

`app/src/updateDownload.ts`：

```ts
export const NSIS_SILENT_FLAGS = ["/S"];
// /D= 必须是**最后一个**参数，且**不能加引号**
```

`installerArguments(appDir)` 产出 `["/S", "/D=D:\\软件\\奥皇上品"]`。

> **`/D=` 必须放最后、不能加引号** —— 这是 NSIS 的硬约定，写错了它会忽略路径，装到 `%LOCALAPPDATA%\Programs` 去，于是「快捷方式指向老版本」的循环就回来了。

---

## 六个坑（全部实测确认，别再踩）

### 1. `/S` 发给 Inno 会弹向导
换 NSIS 后解决。

### 2. `installOnQuit()` 里删掉安装包再启动安装器
旧代码 spawn 完 PowerShell 立刻 `discardInstaller()`，把 900ms 后要运行的那个 exe 删了。
**现在**：清理交给 PowerShell 脚本末尾（装完才删），JS 侧一行不删。

### 3. `before-quit` 里 `void async()` 没有 `preventDefault`
异步读标记 + 异步读 pending 期间，进程已经退干净，PowerShell 根本没被 spawn。
**现在**：`event.preventDefault()` 拦住退出，两步异步走完再 `app.exit(0)`。

### 4. 前端从来没调过 `set-auto-apply`
`update:set-auto-apply` 这个 IPC 在 `web/src` 里 `grep` 为空，`isAutoApplyMarked()` 永远是 false。
**现在**：`update:download` 成功后主进程直接 `markAutoApply()`，不依赖前端。

### 5. `spawn("powershell.exe")` 走 PATH 解析，静默失败
打包环境 PATH 被裁剪，`spawn` 只 emit 一个 `error` 事件，没人监听就完全静默：安装包在、软件优雅退出、`install.log` 一行没有。
**现在**：绝对路径 `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe` + `child.on("error")` 落盘。

### 6. `detached:true` + `stdio:"ignore"` 直连 spawn PowerShell 会秒退 ★最关键
实测六种写法（`_test_spawn*.mjs`）：

| 写法 | 结果 |
|---|---|
| `spawn(ps, args, {detached:true, stdio:"ignore"})` | **FAIL** 进程 exit 0，脚本一行没跑 |
| `spawn(ps, args, {detached:true, windowsHide:true})` | **FAIL** 同上 |
| `spawn(ps, args, {detached:true, stdio:[null,null,null]})` | **FAIL** 同上 |
| `spawn(ps, args, {shell:true})` | OK |
| `spawn(ps, args, {detached:false}).unref()` | OK（但没真脱离） |
| `spawn("cmd", ["/c","start","","/b",ps,...])` | **OK 且真脱离** ← 采用 |

**现在**：

```ts
_spawn(process.env.ComSpec || "cmd.exe",
  ["/c", "start", "", "/b", psExe, "-WindowStyle", "Hidden",
   "-NonInteractive", "-EncodedCommand", encoded],
  { detached: true, stdio: "ignore", windowsHide: true });
```

### 7. spawn 完立刻 `app.exit(0)`，PowerShell 冷启动还没到第一行
PowerShell 冷启动要 300~800ms，主进程一退进程树就被拆。

**两处修**：
- 脚本把 `launching:` 那行日志**提到 `Start-Sleep 900` 之前**，先落盘再等
- `installOnQuit()` 轮询 `install.log` 等 `launching:` 出现（最多 15 秒）再返回

---

## 桌面快捷方式为什么不会指向老版本

`app/build/installer.nsh` 的 `customInstall` 在**每次安装**时用 PowerShell 重建桌面 + 开始菜单快捷方式，带 `--no-sandbox`：

```
目标: D:\软件\奥皇上品\AohuangShangpin.exe
参数: --no-sandbox
```

`customUnInstall` 里对应删除。所以快捷方式永远是「这次装到哪儿就指向哪儿」。

`!macro customInit` **保持为空**：electron-builder 的 `multiUser.nsh` 已经会从注册表 `InstallLocation` 恢复上次目录、并允许 `/D=` 覆盖。我们早先在 `customInit` 里自己 `ReadRegStr` + `StrCpy $INSTDIR`，执行顺序在 `multiUser.nsh` **之后**，把 `/D=` 覆盖掉了 —— 这正是「装到别处 / 快捷方式指向旧目录」的根因。

---

## 验收证据（2026-11-23 实测）

```
[07:27:23] before-quit: requested=false
[07:27:23] auto-apply marked=true
[07:27:23] pending=0.1.42 ...\updates\AohuangShangpin-0.1.42-Setup.exe
[15:27:23] launching: ...\updates\AohuangShangpin-0.1.42-Setup.exe
[15:27:28] setup exit=0
[15:27:30] restart: D:\软件\奥皇上品\AohuangShangpin.exe
```

- 版本：0.1.41 → **0.1.42**
- 进程：关窗后自动恢复到 4 个
- `updates/`：只剩 `auto-apply.json`（安装包 + pending 已清）
- 快捷方式：`D:\软件\奥皇上品\AohuangShangpin.exe --no-sandbox`，目标版本 0.1.42

---

## 老用户为什么必须手动装一次

0.1.33 ~ 0.1.35 携带的是旧更新器（spawn 完就删安装包 + 无 `preventDefault`），**它们的自动更新永远不可能成功**。这批用户需要手动下载安装一次 0.1.42 及以后的版本，之后自动升级才正常。

---

## 发版命令

```bash
cd /d/缓存文件夹/codex工作台/p006/miaoshou
python -X utf8 scripts/check_version.py      # 五处版本一致
pnpm build
cd app && pnpm exec electron-builder --win nsis && cd ..
node scripts/verify-desktop-package.mjs      # 含 /S、/D= 断言
python -X utf8 scripts/publish_release.py "release/奥皇上品-<ver>-Setup.exe" <ver> "更新说明"
python -X utf8 scripts/verify_release.py
python -X utf8 scripts/deploy_server.py
```

`installer/setup.iss`（Inno）已停用，仅为满足五处版本同步检查而保留版本号。
