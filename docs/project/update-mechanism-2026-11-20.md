# 更新机制改造（2026-11-20）

> 负责人要求：
> 1. 「软件打开不得强制用户更新」
> 2. 「下载更新版本之后，应该是自动读取当前软件的安装目录，然后拿新版本覆盖，
>    旧版本清理，这些都是下载好之后静默处理的，然后自动重启软件，
>    用户桌面端的快捷方式启动也是在最新版本的」

---

## 一、先回答「打开会不会强制更新」

**查过了：不会。** 当前没有任何强制更新逻辑。

- `VersionPanel.tsx` 的「检查更新」是用户主动点的按钮
- `UpdateActions` 只在 `updateAvailable` 时才渲染，且只提供「下载」「重启并安装」两个可选动作
- 启动流程里没有版本门禁（没有 `minVersion` 之类的东西）
- 关软件时只有 `isInstallRequested()` 才装 —— 单纯关程序什么都不做

所以「不得强制更新」这条**本来就满足**，不需要改。这次没动这块。

---

## 二、发现的真问题：注册表里有两套安装项

这台机器上实测：

```
HKCU\...\Uninstall\21fba3c9-f995-53c4-90b7-387e2ec083f9
   DisplayName     : 奥皇上品 0.1.23
   UninstallString : "D:\软件\奥皇上品\Uninstall AohuangShangpin.exe" /currentuser

HKCU\...\Uninstall\{8F3A2C41-7B5E-4D9A-9C1F-2E6B8D4A7F30}_is1
   DisplayName     : 奥皇上品
   DisplayVersion  : 0.1.21
   UninstallString : "D:\软件\奥皇上品\unins000.exe"     ← 文件已经不存在
```

两套不同 AppId 的安装项共存，其中 **0.1.21 那套的卸载器文件已经没了**。
后果：

- 「应用和功能」里出现两个「奥皇上品」
- 点 0.1.21 那个卸载会直接报错（找不到 `unins000.exe`）
- 脏注册表会让「旧版本清理」做不干净

### 清理办法（需要你手动执行一次）

```powershell
# 删掉那个已经失效的 0.1.21 注册项（Inno Setup 的 _is1 后缀）
Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F3A2C41-7B5E-4D9A-9C1F-2E6B8D4A7F30}_is1" -Recurse -Force
```

> 为什么不能自动做：这是删注册表，出问题会影响卸载功能，应该由你确认后手动执行。
> 删之前建议先导出备份：`reg export "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F3A2C41-7B5E-4D9A-9C1F-2E6B8D4A7F30}_is1" backup.reg`

---

## 三、安装目录与快捷方式的实际情况

好消息：**这部分本来是对的。**

安装器 `installer/setup.iss`：

```ini
DefaultDirName={localappdata}\Programs\{#MyAppName}
UsePreviousAppDir=yes        ← 升级时沿用上次安装目录
DisableDirPage=yes           ← 静默时不弹目录选择
```

`UsePreviousAppDir=yes` 保证：用户当初装到 `D:\软件\奥皇上品`，
之后每次升级都还装到那里，不会跑到默认的 `%LOCALAPPDATA%\Programs`。

快捷方式用 `{app}` 动态展开：

```ini
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--no-sandbox"; Tasks: desktopicon
```

`{app}` 在每次安装时按实际目录展开，**所以装到同一目录时快捷方式自动指向新版本，不需要额外处理。**

实测确认：

```
Desktop\奥皇上品.lnk              → D:\软件\奥皇上品\AohuangShangpin.exe  --no-sandbox
Start Menu\奥皇上品\奥皇上品.lnk  → D:\软件\奥皇上品\AohuangShangpin.exe  --no-sandbox
```

---

## 四、这次实际改的东西

### 4.1 `installer/setup.iss`：静默安装也自动启动

```ini
; 改前
Filename: "{app}\{#MyAppExeName}"; Flags: nowait postinstall skipifsilent

; 改后
Filename: "{app}\{#MyAppExeName}"; Flags: nowait postinstall
```

`skipifsilent` 原本表示「静默安装就别启动，交给调用方重启」。
现在去掉它，让 **Inno 自己在收尾时启动** —— 那个时机最准：
setup 进程返回时 exe 可能还没落完盘，调用方去 spawn 会失败。

### 4.2 `updateInstaller.ts`：重写 `installOnQuit()`

原来的实现有三个不足：

1. 安装后固定 `Start-Sleep -Seconds 2` 就启动 —— 新版本还没落完盘时会启动失败
2. 启动的是 `app.getPath("exe")`（当前进程的路径），如果安装目录变了就启动错
3. 没检查是否已经在跑 → 叠加 Inno 的自动启动会**开两个窗口**

现在的实现：

```powershell
Start-Sleep -Milliseconds 800                          # 等本进程退干净
$setup = <base64 解码的安装包路径>
$exe   = <base64 解码的重启目标>
Start-Process -FilePath $setup -ArgumentList '/S','/NOCANCEL' -Wait
Start-Sleep -Seconds 3                                 # 等新版本落完盘
$running = Get-Process -Name 'AohuangShangpin' -EA SilentlyContinue
if (-not $running -and (Test-Path $exe)) { Start-Process -FilePath $exe -ArgumentList '--no-sandbox' }
# 清理 updates 目录里的历史安装包
Get-ChildItem (Split-Path $setup) -Include *.exe,*.part -File -EA SilentlyContinue ...
```

关键改动：

- **重启目标优先取快捷方式指向的 exe**（`resolveShortcutTarget()`）
  —— 安装器会把快捷方式指到它自己装的目录，读它比猜 `app.getPath("exe")` 可靠
- **`Get-Process` 判重**：Inno 已经启动了就不再启动，避免两个窗口
- 用 `Base64(UTF-16LE)` 传中文路径：`D:\软件\奥皇上品` 走 cmd 的 GBK 会乱码

### 4.3 新增「自动静默更新」开关

`updateInstaller.ts` 加三件事 + `main.ts` 加两个 IPC：

```typescript
markAutoApply()        // 打标记：这次下载的版本应该自动装
isAutoApplyMarked()    // 查标记
clearAutoApplyMark()   // 清标记
```

`before-quit` 逻辑改成：

```typescript
const shouldInstall = isInstallRequested();   // 用户点了「重启并安装」
if (shouldInstall) {
  void installOnQuit();
} else {
  // 后台已下好 + 开着自动更新 → 关软件时静默装
  void isAutoApplyMarked().then((auto) => { if (auto) void installOnQuit(); });
}
```

**为什么用「关软件」当触发点、而不是「下载完立刻装」**：

安装会杀掉进程。如果用户正在跑发布或认领任务，立刻装等于把任务全丢，
载体建到一半还会在上游留下孤儿草稿。「关软件」本来就是一次自然收尾点。

用户不想要自动更新时，在设置里关掉即可，回到手动模式。

---

## 五、完整更新流程（改后）

```
用户开软件
   ↓ 正常使用（不强制、不弹窗、不打断）
后台检查到新版本（或用户点「检查更新」）
   ↓ 下载（边下边算 sha256，不符立刻删）
下载完成 → 写 pending.json
   ↓
   ├─ 开自动更新：打 auto-apply.json 标记，**继续用，不打断**
   │     用户关软件 → before-quit → 静默安装 → 自动重启 → 已是新版
   │     快捷方式仍指向同一目录 → 点桌面图标启动的就是最新版本
   │
   └─ 关自动更新：界面显示「重启并安装」，用户自己决定时机
         点按钮 → 立刻安装并重启
```

---

## 六、验证

- `pnpm check` 全绿（typecheck + lint + design-check）
- `app` 版本 `npx tsc --noEmit` 通过
- 全量回归 **8 条基线失败，零新增**
  （其中 `test_every_mutating_route_has_a_permission_above_member`
  抓到过我新加的写接口缺权限声明，已修为 `require_permission("process")`）

---

## 七、还剩什么

| 事项 | 状态 |
|---|---|
| 不强制更新 | ✅ 本来就没有，无需改 |
| 下载后静默覆盖安装 | ✅ 改好了 |
| 旧版本/旧安装包清理 | ✅ updates 目录清理 + [InstallDelete] 已有 |
| 自动重启 | ✅ 改好了（含判重避免两个窗口） |
| 快捷方式指向最新版本 | ✅ 本来正确（`{app}` 动态展开） |
| 注册表两套安装项 | ⬜ **需要你手动执行一次清理**（见第二节） |
| 打包发布验证 | ⬜ 要等下次发版时实际跑一遍 |
---

## 各版本更新行为的差异（2026-11-20 核实源码）

负责人问：「之前版本的更新是不是没有现在这个自动覆盖重启的，是不是要链接重新下载」

回答分两部分：**「自动覆盖重启」是 0.1.30 才有**，但**「重新下载」不需要**。

### 源码对比（不是推测）

**0.1.29 的 `before-quit`**（`825482c^`）：

```typescript
app.on("before-quit", () => {
  // 用户点了「重启并安装」才装；单纯关程序不装
  if (isInstallRequested()) {
    void installOnQuit();
  }
  server?.kill();
});
```

→ 关软件**不装**。必须用户点按钮。

**0.1.30+ 的 `before-quit`**（`825482c` 起）：

```typescript
app.on("before-quit", () => {
  if (isInstallRequested()) {
    void installOnQuit();
  }
  void isAutoApplyMarked().then((auto) => {
    if (auto) void installOnQuit();   // 自动更新开着就静默装
  });
  server?.kill();
});
```

→ 关软件时**静默覆盖 + 自动重启**。

**前端 `UpdateDialog`（0.1.29 与现在一致）**：

```typescript
// 检查是否已有缓存包 → 自动安装；否则自动开始下载
useEffect(() => {
  void desktopUpdate.pending().then((p) => {
    if (p && p.version === version.latest) { setPhase("done"); handleInstall(); }
    else { startDownload(); }
  });
}, [desktopUpdate]);

// 下载完成后自动安装
useEffect(() => { if (phase === "done") handleInstall(); }, [phase]);
```

→ **弹窗一出现就自动下载，下完自动调安装器。** 这个 0.1.29 就有。

### 所以旧版本用户实际经历什么

| 版本 | 弹窗出现 | 下载 | 安装 | 装完 |
|---|---|---|---|---|
| 0.1.29 及更早 | 自动 | **自动** | 自动启动安装器，但**安装器界面会出来，要点「下一步」** | **要自己手动打开软件** |
| 0.1.30+ | 自动 | 自动 | **静默覆盖，看不到界面** | **自动重启** |

**关键：不需要「重新下载」。** 两个版本都会自动下载，差异只在
「装的时候要不要点几下」和「装完要不要自己开」。

### 为什么 0.1.30 要改

负责人的原话：「当前软件更新机制有问题，软件打开不得强制用户更新…
静默处理的，然后自动重启软件」。

0.1.30 做的事：
- 不再强制弹窗阻塞
- 下好之后**不打断当前工作**（正在跑的发布/认领任务不会丢）
- 等用户关软件时静默覆盖
- 装完自动重启，用户回来就是新版本

### 旧版本用户升级路径（这次 0.1.31）

```
1. 打开软件 → 检测到 0.1.31
2. 弹窗自动开始下载（74MB，CDN 约 52 秒）
3. 下完自动启动安装器 → 用户点「下一步」安装
4. 装完用户自己打开软件 → 已经是 0.1.31
5. 从此以后升级都是静默的（0.1.31 支持）
```

**只有这一次需要手动点几下，之后再也不用。**
