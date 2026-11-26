# 审计 E：Electron 主进程与更新链

> 审计范围：`app/src/`（6 文件 2 078 行：`main.ts` 606 / `updateInstaller.ts` 731 / `updateDownload.ts` 323 / `sessionVault.ts` 71 / `preload.ts` 70 / `updateDownload.test.mjs` 277）、`app/electron-builder.yml`（50）、`app/package.json`（29）、`scripts/build.ps1`（53）、`scripts/verify-desktop-package.mjs`（241）
> 旁证（只读引用，不作为审计对象）：`server/services/system_service.py`、`server/services/update_check.py`、`server/api/system.py`、`server/core/constants.py`、`scripts/check_version.py`、`scripts/publish_release.py`、`scripts/verify_release.py`、`scripts/release_fixed_link.py`、`web/src/components/layout/UpdateDialog.tsx`、`web/src/lib/apiTarget.ts`、`release/win-unpacked/resources/app.asar`
> 审计方式：只读静态审计 + 真机探针（`node` 复现路径拼接、PowerShell 复现 `-Include` 与 `-ArgumentList` 行为、`Get-AuthenticodeSignature` 查签名、直接读 `app.asar` 内产物）。**未修改任何源码。**
> 审计基准：`CLAUDE.md` §1.1 打包事实、`docs/architecture.md`、`docs/project/fixed-download-link-2026-11-26.md`、`docs/project/auto-update-nsis-2026-11-23.md`、`docs/project/rules.md`
> 已复核通过的既有闸门：`python -X utf8 scripts/check_version.py` → `[版本校验] 通过：四处均为 0.1.54`（exit 0）；`node scripts/verify-desktop-package.mjs` → `Desktop package verified: v0.1.54, ...`（exit 0）；`cd app && node --test src/*.test.mjs` → `pass 29 / fail 0`

---

## 结论摘要

这个客户端的**边界写得比多数商业项目硬**：`contextIsolation: true` / `nodeIntegration: false`、`setWindowOpenHandler` 一律 deny、`will-navigate` 与 `will-redirect` 双重拦截、`isTrustedPage()` 用 `path.resolve` 全等而不是前缀匹配、`validateUpdateUrl` 精确匹配域名而不是 `endsWith`、`sessionVault` 明确拒绝「没有 OS 加密就退回明文」、`verify-desktop-package.mjs` 甚至用 mock electron 在 VM 里真跑一遍 `dist/main.js` 断言 guard 行为。这些不是补丁，是有意识的安全设计。

问题出在**三个信任模型错位**，以及**闸门没站在必经之路上**：

1. **「清单里的字符串」被当成数据，实际被当成路径。** `updateInstaller.ts:299` 用 `manifest.version` 直接拼文件名，而这个值从渲染层一路透传、服务端不校验、客户端不校验。实测 `version = "99..\\..\\..\\..\\..\\..\\Roaming\\...\\Startup\\aohuang"` 能落盘到 Startup 目录，且 `is_newer()` 因为只取首段数字而放行 `99`。**这是本次审计唯一的「写任意路径 + 开机自启」原语。**

2. **「sha256 是完整性锚点」被降级成「TLS 是完整性锚点」。** 客户端走软件内更新时，`sha256` 由**渲染层**传入（`main.ts:376-393`），不是从服务端清单独立取；下载完成后写进 `pending.json`，但 `pendingInstaller()`（`updateInstaller.ts:262-273`）在安装时**只 `fs.access` 存在性，不重校验 sha256**。整条链的完整性等于「渲染层源码没被改」+「TLS 没被破」。而 `resources/web/` 是本机明文可改的目录。

3. **「校验了」与「校验在必经之路上」是两件事。** `verify_release.py` 能挡住 2026-11-26 那类固定链接事故，但 `scripts/build.ps1` 从头到尾**不调用它**（全仓 `package.json` / `*.ps1` / `*.bat` 无一处引用 `publish_release` 或 `verify_release`）。`verify-desktop-package.mjs` 能挡住「asar 版本 stale」，但它只比版本号，**不比 `dist/*.js` 与 `src/*.ts` 的对应关系**——结果 `app/dist/installerArgs.js`（2026-11-23 的 Inno `/VERYSILENT` 死代码，`src/` 里连这个文件都已删除）至今在 `app.asar` 里躺着随包分发。

4. **文档声称的四个能力，代码里一个都没有。** `docs/architecture.md` 三处写「单实例锁」（L16 / L54 / L760）、L20 写「退出时优雅关停后端（SIGTERM → 5s → kill）」、L19 写「electron-updater」、L484 写「落盘到 `userData/local-token.bin`」。实测：全仓 `requestSingleInstanceLock` / `second-instance` / `SIGTERM` / `taskkill` / `getppid` 命中数为 0，`app.asar` 内 `dist/main.js` 同样为 0；`token.ts` / `updater.ts` / `index.ts` 三个文件不存在。这不是「文档没更新」，是**架构文档把一个不存在的实现描述成了已有能力**——它会让下一个人（和下一个 AI）在错误的基线上做决策。

5. **两处「为了修好而引入的判据」本身就是新 bug。** `waitForLaunchMarker()` 判 `install.log` 里有没有 `launching:`（`updateInstaller.ts:588`），而 `install.log` 是 append-only 的（`main.ts:586` / `updateInstaller.ts:568` 都是 `appendFile`，全仓无 truncate）——只要历史上成功安装过一次，这个 15 秒等待就**永远立刻返回 true**，主进程随即 `app.exit(0)`，2026-11-23 那个「PowerShell 还没起来主进程就死了」的坑原样复活。同理 `installPowerShellScript()` 第 310 行的清理命令实测**一个文件都删不掉**（`Get-ChildItem -Path <dir> -Include *.exe -File` 不带 `-Recurse` 也不带尾部通配符时返回 0 个对象，已用真 PowerShell 验证）。

**P0 共 6 项，P1 共 8 项，P2 共 9 项，合计 23 项。** 性质判定：**根因 8 项**（E-P0-1/2/3/6、E-P1-1/6/7、E-P2-4）、**补丁叠补丁 10 项**（E-P0-5 兼属、E-P1-2/4/8、E-P2-1/2/5/6/7/8）、**跷跷板 3 项**（E-P0-4、E-P1-3、E-P1-5）、文档债 1 项（E-P2-3）、可维护性 1 项（E-P2-9）。

补丁叠补丁占 43% 这个比例本身就是结论：**这个模块的历史修法一直是「就近加一个判断」，没有一次把「谁才是唯一权威」重新划清。** 三个具体的「权威重叠」点：安装包该由谁删（三套机制，见 E-P1-5）、清单字段该由谁校验（客户端/服务端/渲染层三套，见 E-P1-8）、日志判据该由谁界定作用域（见 E-P0-4）。

最该先修的三个：**E-P0-1**（`version` 路径穿越，唯一能持久化的原语）、**E-P0-2**（sha256 锚点被渲染层持有 + 安装前不重校验）、**E-P0-3**（单流下载的写流错误不可感知 —— 一种模式产出「校验通过但文件残缺」的安装包，另一种模式**直接打死主进程**，两种都已真机复现）。

---

## P0 问题（安全 / 功能失效）

### E-P0-1 `manifest.version` 未做任何校验就参与路径拼接，可写入 `updates/` 之外任意 `*-Setup.exe`，包含用户 Startup 目录（开机自启持久化）

- **证据 1：拼接点。** `app/src/updateInstaller.ts:299`
```ts
  const target = path.join(dir, `AohuangShangpin-${manifest.version}-Setup.exe`);
```

- **证据 2：`version` 只在下载完成后被原样写进 `pending.json`，没有任何形状约束。** `app/src/updateInstaller.ts:377-381`
```ts
    await fs.writeFile(
      path.join(dir, "pending.json"),
      JSON.stringify({ path: finalPath, version: manifest.version, sha256: hashHex,
                       downloadedAt: Date.now() }),
      "utf-8",
    );
```

- **证据 3：同一函数对 `url` 和 `sha256` 都有显式闸门，唯独 `version` 没有。** `app/src/updateInstaller.ts:285-295`
```ts
  const safeUrl = validateUpdateUrl(manifest.url);
  if (!safeUrl) {
    return {
      ok: false,
      error: `更新地址不合法（只允许 ${ALLOWED_HOSTS.join(" / ")} 的 https .exe 地址）`,
    };
  }
  const expected = String(manifest.sha256 || "").trim().toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(expected)) {
    return { ok: false, error: "更新清单缺少有效的 sha256，拒绝下载" };
  }
```
—— `url` 有白名单、`sha256` 有正则，`version` 是三者里唯一被拿去拼路径的，却是唯一零校验的。**这就是「校验口径不覆盖真正危险的那个变量」的教科书形态。**

- **证据 4：`version` 的来源是渲染层，不是服务端独立取的值。** `web/src/components/layout/UpdateDialog.tsx:67-73`
```ts
    const manifest = {
      version: String(version.latest || ""),
      url: String(version.url || ""),
      sha256: String(version.sha256 || ""),
      sizeBytes: version.sizeBytes ?? undefined,
      notes: version.notes ?? undefined,
    };
```
→ `app/src/main.ts:376-384` 直接收下：`ipcMain.handle("update:download", async (event, manifest: UpdateManifest) => { if (!trustedSender(event)) return ...; if (!manifest || typeof manifest !== "object") return ...; const result = await downloadInstaller(manifest, broadcastProgress);` —— 只检查「是个对象」。

- **证据 5：服务端也不校验 `version`。** `server/services/system_service.py:103-112`
```python
    latest = str(manifest.get("version") or "").strip()
    download = str(manifest.get("url") or "").strip()
    base.update({
        "ok": True,
        "latest": latest,
        "updateAvailable": bool(latest and download and is_newer(latest, current)),
        "url": download or None,
        "sha256": str(manifest.get("sha256") or "") or None,
        "sizeBytes": manifest.get("sizeBytes"),
```
`is_newer` 的判定口径更是宽到等于没有：`server/services/system_service.py:42-53`
```python
def parse_version(value: Any) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(value or "").strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)
```
→ 只取**每一段里的数字**，遇到无数字的段就 `break`。所以 `"99..\\..\\..\\pwn"` 解析成 `(99,)`，`is_newer("99..\\..", "0.1.54")` = True（已实测）。**只要把恶意 version 的第一段写成 `99`，服务端的 `updateAvailable` 闸门就放行。**

- **证据 6：真机复现（`_build_tmp/audit_e_traversal3.mjs`，`path.join` 参数与 L299 完全一致的副本）。**
```
case : 正常版本号（对照）
  version  : "0.1.55"
  拼出的原始: C:\Users\Administrator\AppData\Local\aohuang-desktop\electron\updates\AohuangShangpin-0.1.55-Setup.exe
  仍在 updates/ 内: true

case : 以 99 开头过 is_newer 闸门 + 6 级上行进 Startup
  version  : "99..\\..\\..\\..\\..\\..\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\aohuang"
  拼出的原始: C:\Users\Administrator\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\aohuang-Setup.exe
  仍在 updates/ 内: false
  .part 路径: C:\Users\Administrator\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\aohuang-Setup.exe.part
```
（`updatesDir()` 见 `updateInstaller.ts:64-66`：`path.join(app.getPath("userData"), "updates")`；上面 base 取的是实测的 userData 展开值。）

- **证据 7：还有一条路径不需要穿越就能删任意文件。** `app/src/updateInstaller.ts:391` 的 catch 与 `L341` 的分段失败回退分支都无条件 `fs.rm(partial, { force: true })`，而 `partial` 是 `${target}.part` 的穿越形态 —— 即「删除磁盘上任意 `*-Setup.exe.part`」。

- **证据 8：写进去的东西会被自动执行。** `app/src/main.ts:589-599`
```ts
      const marked = await isAutoApplyMarked();
      trace(`auto-apply marked=${marked}`);
      const shouldInstall = isInstallRequested() || marked;
      if (shouldInstall) {
        const pending = await pendingInstaller();
        trace(`pending=${pending ? `${pending.version} ${pending.path}` : "null"}`);
        await installOnQuit();
```
而 `installOnQuit()` 用的是 `pending.path`（`updateInstaller.ts:491`），不看它在不在 `updates/` 里。配合下面 E-P0-4 的 `markAutoApply` 自动打标（`main.ts:391`），「下载成功 → 关软件 → 执行该路径的 exe」是全自动的。

**触发前提（必须写清楚，不含糊）：** 需要攻击者能控制 `version.json` 的内容（或控制 `aohuang888.com` / `aohuang888.xyz` 上的文件）。仅靠客户端本地无法凭空构造 manifest。但控制一份 CDN 上的 JSON 的门槛，远低于控制一个已签名安装包的字节——后者有 sha256 挡着，前者没有。**sha256 挡的是「安装包内容被篡改」，完全挡不住「清单字段被篡改导致写到别处」。这是两个不同维度，而这套代码只防了第一个。**

**性质：根因。** 不是补丁叠补丁，也不是跷跷板——从第一天起 `version` 就没被当成不可信输入。同一份代码对 `url` / `sha256` 都设了闸门，说明作者有「清单不可信」的意识，只是漏了这个变量参与路径拼接这件事。

**修复方向（不在本次改动范围内，仅记录）：** `downloadInstaller()` 入口加 `if (!/^[0-9]{1,4}(\.[0-9]{1,4}){1,3}$/.test(String(manifest.version || ""))) return { ok: false, error: "版本号格式非法" };`，并在 `pendingInstaller()` 里对 `data.path` 做 `path.resolve(data.path).startsWith(path.resolve(updatesDir()) + path.sep)` 断言。

---

### E-P0-2 完整性锚点被渲染层持有：`sha256` 由前端传入而非服务端独立取；且安装前完全不重校验，`pending.json` 里写下的 `sha256` 字段没有任何读者

- **证据 1：`sha256` 的入口只有一个，来自渲染层。** `app/src/preload.ts:28-34`
```ts
    download: (manifest: {
      version: string;
      url: string;
      sha256: string;
      sizeBytes?: number;
      notes?: string;
    }) => ipcRenderer.invoke("update:download", manifest),
```
主进程侧 `app/src/main.ts:376-384` 只做 `trustedSender` + `typeof manifest !== "object"`。**没有任何一处从 `REMOTE_API_BASE` 或 `version.json` 自己再取一次 `sha256` 做交叉比对。**

- **证据 2：它信任的页面，是本机明文可改的目录。** `app/electron-builder.yml:15-17`
```yaml
extraResources:
  - from: ../web/dist
    to: web
```
`app/src/main.ts:177-187` 的 `isTrustedPage()` 认的就是 `path.resolve(process.resourcesPath, "web", "index.html")`。`resources/web/` 是普通文件，任何有该用户权限的进程都能改。**于是「sha256 校验」在软件内更新这条链上的实际强度 = 「本机 web 资源未被篡改」，而不是「服务端清单未被篡改」。**

- **证据 3：`pending.json` 里存了 sha256，但读的时候完全不用。** 写：`app/src/updateInstaller.ts:377-381`（见 E-P0-1 证据 2）。读：`app/src/updateInstaller.ts:262-273`
```ts
export async function pendingInstaller(): Promise<{ path: string; version: string } | null> {
  try {
    const marker = path.join(updatesDir(), "pending.json");
    const raw = await fs.readFile(marker, "utf-8");
    const data = JSON.parse(raw) as { path?: string; version?: string };
    if (!data.path || !data.version) return null;
    await fs.access(data.path);
    return { path: data.path, version: data.version };
  } catch {
    return null;
  }
}
```
—— 返回类型里连 `sha256` 都不带；`fs.access` 只验证「存在且可访问」。**写进去的字段没有消费者，等于没写。**

- **证据 4：从「校验通过」到「执行安装」之间有一段任意长的时间窗。** `main.ts:391`（下载成功即打标记）→ `main.ts:587-596`（用户关软件才装）。中间是小时到天级的间隔，`updates\*.exe` 是普通文件，可被替换/截断/替换成同名不同内容。

- **证据 5：唯一的「执行前»闸门只比路径。** `app/src/main.ts:406-412`
```ts
ipcMain.handle("update:run-installer-now", async (event, installerPath: string) => {
  if (!trustedSender(event)) return { ok: false, error: "不可信的调用来源" };
  // 只允许跑我们自己下到 updates 目录里的 exe，不接受任意路径
  const pending = await pendingInstaller();
  if (!pending || path.resolve(pending.path) !== path.resolve(String(installerPath))) {
```
—— 校验的是「路径等于 pending.path」，而 `pending.path` 本身已经是未经内容校验的值。

**性质：根因（信任模型错位）。** 完整性的判定权被放在了「被保护方」手里。分段路径的 hash 是从落盘文件重算的（`updateInstaller.ts:192-199`，`handle.close()` 后重新读文件），说明作者知道「hash 必须绑定落盘内容」——但这条纪律只落实到了下载环节，没落实到安装环节。

**修复方向：** 主进程自己在 `downloadInstaller()` 之前调一次 `version.json`（或让后端在 `/system/version` 响应里带一个服务端签名的 sha256），与渲染层传入的值交叉比对；`pendingInstaller()` 返回 sha256 并在 `installOnQuit()` 里对 `pending.path` 重算一次再比。

---

### E-P0-3 `downloadSingleStream` 没有写流 `error` 监听：写入阶段失败 → `out.end(cb)` 照样 resolve 成「sha256 校验通过」；打开阶段失败 → unhandled `'error'` 打死整个主进程（两种模式均已真机复现）

- **证据 1：写流只在两个地方被触碰，都没有 `error` 监听。** `app/src/updateInstaller.ts:227-250`
```ts
        const out = createWriteStream(partialPath);
        response.on("data", (chunk: Buffer) => {
          received += chunk.length;
          hash.update(chunk);
          out.write(chunk);
          ...
        });
        response.on("end", () => out.end(() => resolve()));
        response.on("error", (error: Error) => {
          out.destroy();
          reject(error);
        });
```
全文件 `grep -n "out\.on\|out\.once"` → **无命中**（只有 L227 `createWriteStream`、L231 `out.write`、L243 `out.end`、L245 `out.destroy`）。`out.write(chunk)` 的返回值被丢弃，背压不处理。

- **证据 2：真机探针复现两种失败模式（`_build_tmp/audit_e_writestream2.mjs`，形状与源码 227-250 一致）。**

模式一 —— **打开成功、写入阶段失败 → 假成功**：
```
[场景A 打开后出错] error=simulated ENOSPC endCallbackFired=true
```
`end` 的 callback **照样被调用**，`resolve()` 执行，`downloadSingleStream` 返回 `{ok: true, sha256: <完整内容的 hash>}`。因为 hash 是从网络 chunk 算的（`hash.update(chunk)` 在 `out.write` 之前），它对得上 expected。**结果：「校验通过」+ 残缺文件。**

模式二 —— **打开阶段就失败 → 整个主进程崩溃**：
```
[场景B] end 的 callback 被调用了 —— 写失败被当成功
node:events:505
    throw er; // Unhandled 'error' event
    ^
Error: ENOENT: no such file or directory, open '...\nodir\x.bin'
    at emitErrorNT (node:internal/streams/destroy:170:16)
Node.js v26.13.0
[exit=1]
```
进程是被 **unhandled 'error' 事件**打死的，不是被 try/catch 接住——`createWriteStream` 的 open 是异步的，错误在后续 tick 才 emit，落在 Promise executor 之外，`downloadSingleStream` 外层那个 `try { ... } catch` 完全接不到。**在 Electron 主进程里这就等于整个应用崩溃（窗口消失），而不是「更新失败」**。

- **证据 3：模式二的触发路径就摆在 E-P0-1 旁边。** `fs.mkdir` 只保证 `updates/` 存在（`updateInstaller.ts:297-298` `await fs.mkdir(dir, { recursive: true })`），而 `partialPath` 是 `${target}.part`，`target` 来自 L299 的路径拼接——`manifest.version` 一旦带层级（穿越串），父目录不存在 → open ENOENT → **主进程崩溃**。也就是说 E-P0-1 不止能写文件，还能把客户端打成崩溃循环（下次启动 `pruneStaleInstallers` 依旧跑，用户看到的是「一开就闪退」）。

- **证据 4：hash 的来源与落盘脱钩。** 同一段里 `hash.update(chunk)` 用的是**从网络收到的 chunk**，落盘是 `out.write(chunk)` 的另一条路径。所以「磁盘满 / 写权限丢失 / 文件被占用」时：`hash` 是完整内容的 hash（对得上），文件是不完整的。

- **证据 5：校验只比 hash，不比文件大小。** `app/src/updateInstaller.ts:356-362`
```ts
    if (hashHex !== expected) {
      await fs.rm(partial, { force: true });
      return {
        ok: false,
        error: `安装包校验失败（期望 ${expected.slice(0, 12)}… 实际 ${hashHex.slice(0, 12)}…）`,
      };
    }
```
`fs.stat(finalPath)` 在 `updateInstaller.ts:376` 才被调用，且只用于 `size` 字段回填，**没有与 `total` / `received` 比对**。

- **证据 6：分段路径没有这个问题，形成刺眼的不对称。** `app/src/updateInstaller.ts:188-199`
```ts
    await handle.close();
    file = null;
    onProgress?.({ received: total, total, percent: 100 });

    const digest = createHash("sha256");
    const stream = createReadStream(partialPath);
    await new Promise<void>((resolve, reject) => {
      stream.on("data", (chunk) => digest.update(chunk));
      stream.on("end", () => resolve());
      stream.on("error", reject);
    });
    return { ok: true, sha256: digest.digest("hex") };
```
—— `handle.close()` 之后**重新打开落盘文件**算 hash，且有 `stream.on("error", reject)`。所以：分段路径的 hash 绑定文件内容且错误可感知；单流路径的 hash 绑定网络内容且错误不可感知。**同一个「sha256 校验」，两条路径两种强度。**

- **证据 7：后果不是「下载失败」，是「静默装不上」。** 残缺 exe 通过校验 → 写 `pending.json` → 用户关软件 → `installOnQuit()` → NSIS 自身有 CRC 检查，安装器报错退出 → PowerShell 脚本 `$code = if ($proc.HasExited) { $proc.ExitCode } else { -1 }` 记一行日志（`updateDownload.ts:298`），但界面上什么都看不到 → 用户下次开软件还是老版本，`pruneStaleInstallers()` 这次会把它删掉（`updateInstaller.ts:706-724`，非 pending 的 `.exe`/`.part` 全删），于是**证据也消失了**。

- **证据 8：`downloadSingleStream` 不只是兜底路径——它是「清单没给 `sizeBytes`」时的唯一路径。** `app/src/updateInstaller.ts:333`
```ts
  const segments = total > 0 ? planSegments(total) : [];
```
`total` 来自 `Number(manifest.sizeBytes) || 0`（`updateInstaller.ts:301`），而 `sizeBytes` 在渲染层是 `version.sizeBytes ?? undefined`（`UpdateDialog.tsx:71`）、在服务端是 `manifest.get("sizeBytes")` 原样透传（`system_service.py:111`）。**清单里少写一个字段，全部用户就走单流，也就是走上面两条失败模式。**

**性质：根因。** 不是某次补丁引入的——「写流的生命周期」这个对象在两条下载路径上被建模了两次，一次对（分段：close → 重开 → 算 hash → 监听 error），一次错（单流：边收边写 → 从内存算 hash → 不监听 error）。补丁叠补丁的痕迹在于：作者已经知道「hash 必须绑落盘内容」（分段路径的写法就是证据），却没有回头统一单流路径。

**修复方向：** `out.on("error", reject)`（这一条单独就能把「主进程崩溃」降级成「下载失败」）；`hash` 改为下载完成后重开文件计算（与 `updateInstaller.ts:192-199` 一致）；`fs.stat(finalPath).size !== (total || received)` 时拒绝并删 `.part`。

---

### E-P0-4 `waitForLaunchMarker()` 用一个 append-only、永不 truncate 的日志做「PowerShell 真的起来了」判据 —— 只要历史上成功装过一次，这个 15 秒等待此后永远立即返回 true

- **证据 1：判据读整份日志、用 `includes`。** `app/src/updateInstaller.ts:584-593`
```ts
async function waitForLaunchMarker(logPath: string, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const content = await fs.readFile(logPath, "utf-8").catch(() => "");
    if (content.includes("launching:")) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return false;
}
```
—— 没有「从第几字节开始读」的偏移，没有「本次运行」的标识。

- **证据 2：日志只追加，全仓无 truncate。** `app/src/updateInstaller.ts:568`（spawn 失败时 `fs.appendFile`）、脚本内 `app/src/updateDownload.ts:291`
```ts
    "Add-Content -LiteralPath $log -Value ('[' + (Get-Date -Format s) + '] launching: ' + $setup)",
```、`app/src/main.ts:586`
```ts
        fs.appendFileSync(traceLog, `[${new Date().toISOString()}] ${line}\n`);
```
`grep -n "truncate\|writeFile.*install.log\|rm.*install.log" app/src/` → 无命中。

- **证据 3：这个等待是「修复 2026-11-23 第四个坑」的补丁本体，注释把因果写得很清楚。** `app/src/updateInstaller.ts:555-566`
```ts
  // ## 必须等它真的跑起来（2026-11-23 第四个坑）
  //
  // spawn 只是把进程创建请求交给系统，PowerShell 自己冷启动要 300~800ms。
  // 调用方 `before-quit` 紧接着就 `app.exit(0)`，整个进程树被拆掉时
  // PowerShell 还没执行到第一行 —— 表现为：日志只有我们的 trace、
  // `launching:` 一行都没有、安装包原地不动、版本不变。
  //
  // 判据用脚本自己写的那行 `launching:`：它落盘就证明 PowerShell 已经
  // 加载完、脚本已开始执行，之后主进程退出不会影响它（已 detached）。
  const started = await waitForLaunchMarker(logPath, 15000);
```
—— 判据的设计是对的（要的是「脚本真的执行到第一行」），bug 在于**判据的作用域没有限定到本次运行**。

- **证据 4：主进程紧接着就 `app.exit(0)`，不 await 其它任何东西。** `app/src/main.ts:601-605`
```ts
      trace(`quit-install failed: ${error instanceof Error ? error.message : String(error)}`);
    }
    server?.kill();
    app.exit(0);
  })();
```
所以「等待」一旦失效，就是 2026-11-23 那个坑原样重放：PowerShell 冷启动 300~800ms，主进程已经没了。

- **证据 5：失效会在「第一次成功之后」永久生效，且没有任何告警。** 首次升级成功 → `install.log` 里留下 `launching:` → 之后每次升级 `waitForLaunchMarker` 第一次循环（约 0ms）就返回 true → `started = true` → 不再走 L566-571 的 `powershell did not reach launch marker within 15s` 告警分支。

**性质：跷跷板。** 这是教科书形态：为了修「等太久/没等」加了等待（好），等待的判据选了一个全局共享、跨运行累积的状态（坏）。修 A 引入 B，而 B 的症状与 A 完全一致，导致下次出问题时会被误判成「A 没修好」。

**修复方向：** 启动前记录 `install.log` 的当前字节长度，`waitForLaunchMarker` 只读该偏移之后的增量；或每次运行生成唯一 `runId` 并让脚本写 `launching: <runId>`。

---

### E-P0-5 没有单实例锁（`docs/architecture.md` 三处声称有），双开会同时跑两套退出安装流程、互相删除对方的安装包

- **证据 1：全仓无单实例实现。** `grep -rn "requestSingleInstanceLock\|second-instance\|makeSingleInstance" app/ scripts/ --include=*.ts --include=*.js --include=*.mjs --include=*.json`（排除 `node_modules` / `app/dist` / `release/`）→ **无命中**。`app.asar` 内 `dist/main.js` 同样：`requestSingleInstanceLock => false` / `second-instance => false`（用 `@electron/asar` 直接读 `release/win-unpacked/resources/app.asar` 验证）。

- **证据 2：文档三处声称有。** `docs/architecture.md:16`
```
│  · BrowserWindow / 单实例锁 / 原生菜单                                        │
```
`docs/architecture.md:54`
```
| 壳 | `app/` | 窗口、单实例、sidecar 生命周期、密钥注入、自动更新 | 不写业务逻辑，不直连 SQLite |
```
`docs/architecture.md:760`
```
| 并发 | 单实例锁；第二个实例唤起已有窗口，不重复起 sidecar |
```
`docs/architecture.md:82` 还指向一个不存在的文件：`│  │  │  ├─ index.ts            # app.whenReady、单实例锁、窗口`（实测 `app/src/index.ts` 不存在）。

- **证据 3：双开时的第一个冲突出在启动第一行。** `app/src/main.ts:508`
```ts
  void pruneStaleInstallers();
```
而 `pruneStaleInstallers()` 的语义是「除了 `pending.json` 指向的那个，`updates/` 里所有 `.exe` / `.part` 全删」：`app/src/updateInstaller.ts:706-719`
```ts
export async function pruneStaleInstallers(): Promise<void> {
  try {
    const dir = updatesDir();
    const pending = await pendingInstaller();
    const keep = pending ? path.resolve(pending.path) : "";
    const entries = await fs.readdir(dir).catch(() => [] as string[]);
    for (const name of entries) {
      const lower = name.toLowerCase();
      if (!lower.endsWith(".exe") && !lower.endsWith(".part")) continue;
      const full = path.resolve(path.join(dir, name));
      if (keep && full === keep) continue;
      await fs.rm(full, { force: true }).catch(() => {});
    }
```
→ 实例 A 正在下载（`.part` 在写），实例 B 启动 → 把 A 的 `.part` 删了 → A 的 `fetchSegment` 写到一个已 unlink 的句柄上（Windows 上会 EBUSY 或写入孤儿 inode），最终 sha256 不符或异常退出。

- **证据 4：第二个冲突在退出时。** 两个实例都有 `before-quit` 处理器（`main.ts:555`），都读同一份 `pending.json`、同一份 `auto-apply.json`，都 spawn 一个 PowerShell 跑同一个 `$setup`。脚本里最后两行是 `Remove-Item -LiteralPath $setup -Force` 与 `Remove-Item -LiteralPath (Join-Path (Split-Path -LiteralPath $setup) 'pending.json') -Force`（`updateDownload.ts:311-312`）→ **先跑完的那个把另一个要执行的文件和标记一起删了。**

- **证据 5：`updateDownload.ts:310` 的「清理同目录其它安装包」实测完全失效。** 
```ts
    "Get-ChildItem -Path (Split-Path -LiteralPath $setup) -Include *.exe,*.part -File | Where-Object { $_.FullName -ne $setup } | Remove-Item -Force",
```
真 PowerShell 验证（临时目录里放 `a.exe` + `b.part`）：
```
--- Get-ChildItem -Include without -Recurse ---
0
--- with trailing wildcard ---
2
--- plain ---
2
```
→ 不带 `-Recurse`、`-Path` 也不带尾部通配符时，`-Include` **返回 0 个对象**。这条清理是死代码。它本该是双开场景的第二道防线，实际一道都不设。

**性质：根因（能力缺失）+ 补丁叠补丁（三套「谁负责删安装包」的机制——`pruneStaleInstallers` / PowerShell 内联清理 / `discardInstaller`——互相不知道对方存在，其中一套还是死的）。**

**修复方向：** `app.whenReady()` 之前加 `if (!app.requestSingleInstanceLock()) { app.quit(); return; }`，并给 `second-instance` 挂「聚焦已有窗口」；`pruneStaleInstallers` 改为只删 `mtime` 早于 N 分钟的文件。

---

### E-P0-6 sidecar 退出是裸 `kill()`：无 SIGTERM 语义、无 5 秒窗口、无进程树清理、无父进程看门狗 —— Electron 崩溃即留下孤儿 `aohuang-server.exe` 占着 8812 和数据库；而 `docs/architecture.md` 明确写着「SIGTERM → 5s → kill」

- **证据 1：退出时唯一的动作是 `server?.kill()`，紧跟着就是 `app.exit(0)`。** `app/src/main.ts:603-604`
```ts
    server?.kill();
    app.exit(0);
```
`ChildProcess.kill()` 在 Windows 上等于 `TerminateProcess`——**没有任何优雅可言**（Node 文档：Windows 不支持信号，`kill()` 直接终止）。后端收到的不是 SIGTERM，是句柄被砍。

- **证据 2：全仓无信号/异常兜底。** `grep -n "process.on(\|SIGTERM\|SIGINT\|taskkill\|uncaughtException\|render-process-gone\|child-process-gone" app/src/*.ts` → **无命中**。`app.asar` 内 `dist/main.js`：`SIGTERM => false` / `taskkill => false` / `job => false`。也就是说：
  - 主进程被任务管理器结束 → `before-quit` 不触发 → `server?.kill()` 不执行 → **后端 100% 存活成孤儿**；
  - 渲染进程崩溃 → 无 handler；
  - 主进程未捕获异常 → 无 handler。

- **证据 3：后端没有父进程看门狗。** `grep -rn "getppid\|os.getppid\|parent_pid\|PPID" server/ --include=*.py` → 无命中。后端不知道自己被谁拉起，也就不会在父进程消失时自杀。

- **证据 4：`spawn` 没有 `detached`，但也没有 job object 绑定。** `app/src/main.ts:157-165`
```ts
  server = spawn(bundled, [], {
    stdio: ["ignore", out, out],
    windowsHide: true,
    env: {
      ...process.env,
      AOHUANG_API_PORT: String(API_PORT),
      AOHUANG_DATA_DIR: dataDir,
    },
  });
```
Windows 上父进程死亡**不会**级联终止子进程（除非用 Job Object 且设置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）。这里两者都没有。

- **证据 5：文档的承诺。** `docs/architecture.md:20`
```
│  · 退出时优雅关停后端（SIGTERM → 5s → kill）                                   │
```
`docs/architecture.md:18` 还声称「生成并注入本地 token（safeStorage 加密落盘）」、L484 写「落盘到 `userData/local-token.bin`」——实测 `token.ts` 不存在，`sessionVault.ts` 落的是 `operator-session.v1.json`（`main.ts:331-335`），而且那是**用户会话**不是**本地 API token**。**同一段文档里三个能力，两个不存在、一个张冠李戴。**

- **证据 6：端口探测让孤儿进程的后果变成「静默连上错的实例」。** `app/src/main.ts:89-97`
```ts
async function detectApiPort(): Promise<number> {
  for (let port = API_PORT; port <= API_PORT_MAX; port += 1) {
    if (await ping(`http://127.0.0.1:${port}/api/v1/system/health`)) {
      return port;
    }
  }
  // 都探不到时退回首选端口，由调用方按失败处理
  return API_PORT;
}
```
→ 上一个实例的孤儿后端还占着 `8812`，新实例 `startServer()` 起的后端被迫改到 `8813`，但 `detectApiPort()` **从 8812 开始扫、第一个响应就返回** → 新客户端连到的是**旧后端实例**（旧进程、旧 `AOHUANG_DATA_DIR`、旧环境变量）。窗口能打开、接口能响应，但数据和配置是上一份。这是「孤儿进程」从「浪费资源」升级成「**功能错误且不可见**」的那一步。

- **证据 7：这条链在打包态被 `useRemoteServer()` 短路，但验收环境正好会走。** `app/src/main.ts:297-303`
```ts
function useRemoteServer(): boolean {
  if (app.isPackaged) return true;
  const flag = (process.env.AOHUANG_LOCAL_BACKEND || "").trim();
  if (flag === "1") return false;      // 显式要求本地后端（开发/单机测试）
  if (flag === "0") return true;
  return !isDev;                        // 生产默认连服务器，开发默认本地
}
```
正式包恒走远程（与 `CLAUDE.md` §1.1 一致，这部分是**正常设计，不算 bug**）。但 `AOHUANG_LOCAL_BACKEND=1` 正是单机测试/验收模式，`pnpm dev` 也是。**代码路径随 `app.asar` 一起分发**，任何一次「用本地后端验收侧边车」都会踩到。

**性质：根因（实现缺失）+ 文档与现实漂移。** 单独看，孤儿进程是 P1 级别的资源泄漏；和证据 6 的端口扫描顺序组合起来才是 P0——因为它会让新实例静默连到旧后端，产生不可见的错误结果。

**修复方向：** `before-quit` 里先 `server.kill('SIGTERM')` 并 `await` 一个 5 秒竞速 + `taskkill /T /F /PID`；后端加 `AOHUANG_PARENT_PID` 环境变量 + 看门狗线程；`detectApiPort()` 改为「自己 spawn 的 PID 上报的端口」或「从大到小扫/校验实例 ID」。

---

## P1 问题

### E-P1-1 `redirect: "follow"` 三处，域名白名单只覆盖第一跳 —— 服务端代码是逐跳重校验的，客户端不是

- `app/src/updateInstaller.ts:102`（`fetchSegment`）
```ts
      redirect: "follow",
```
- `app/src/updateInstaller.ts:220`（`downloadSingleStream`）
```ts
      const request = net.request({ url, redirect: "follow" });
```
- `app/src/main.ts:456`（`fetchImageBuffer`，剪贴板抓图）
```ts
    const request = net.request({ url: parsed.href, redirect: "follow" });
```

对照服务端同类实现 `server/services/update_check.py:131-148`——注释直接点出为什么不能 `follow_redirects=True`：
```python
def _stream(url: str, *, host_whitelist: set[str] | None) -> Iterator[httpx.Response]:
    ...
    httpx 的 `follow_redirects=True` 会在内部跟完跳转才把最终响应交给我们，
```
并以 `follow_redirects=False` + 循环内逐跳 `validate_download_url` 实现。

影响分级：
- 对安装包下载，**sha256 兜住了内容**（跳到别处下载到的东西 hash 对不上），所以不是 P0；
- 对 `clipboard:copy-image`，**没有任何兜底**：一个 `https://aohuang888.com/...` 的 302 可以把主进程导向任意地址（含 `http://127.0.0.1:xxxx`、内网 IP、`file:` 之外的任意 scheme 由 `net` 支持的范围），把响应字节塞进剪贴板。上限 32MB（`main.ts:441`），无内容类型校验。这是一条「渲染层 → 主进程」的内网可达性原语。

**性质：根因。** 同一份仓库里已经有正确写法（`update_check.py`），客户端没跟上。

---

### E-P1-2 `app:boot` / `app:boot-sync` 是唯一两个没有 `trustedSender` 的 IPC，而它们返回 `bootInfo`（含 `token`）

- `app/src/main.ts:327-329`
```ts
ipcMain.handle("app:boot", () => bootInfo);
ipcMain.on("app:boot-sync", (event) => {
  event.returnValue = bootInfo;
});
```
- 对比同文件里紧随其后的正确写法 `app/src/main.ts:336-340`
```ts
ipcMain.on("session:read", (event) => {
  event.returnValue = event.sender === mainWindow?.webContents
    && event.senderFrame === mainWindow.webContents.mainFrame
    && isTrustedPage(event.senderFrame.url) ? sessionVault.read() : null;
});
```
- `bootInfo` 的结构：`app/src/main.ts:280-285`
```ts
const bootInfo: { apiBase: string; token: string; version: string; platform: string } = {
  apiBase: `http://127.0.0.1:${apiPort}`,
  token: "",
  version: app.getVersion(),
  platform: process.platform,
};
```
- **当前实际危害低**：打包态 `bootInfo.token` 被显式清空（`main.ts:512` `bootInfo.token = "";`，`verify-desktop-package.mjs:198` 还专门断言了这一点）。但 `AOHUANG_LOCAL_BACKEND=1` / `pnpm dev` 路径下 `bootInfo.token = await loadToken()`（`main.ts:533-536`）读的是后端 `secret.key`——**真实令牌，且这是后端管理级共享密钥**。此时任何能触达 `ipcRenderer.sendSync("app:boot-sync")` 的上下文（devtools、被注入的 iframe、`will-navigate` 放行前的中间态）都能拿到。
- `preload.ts:13` 还直接把 `process.platform` 暴露出去，说明 preload 自身在 `contextIsolation: true` 下运行，主进程侧的 guard 是唯一防线。

**性质：补丁叠补丁。** 其余 15 个通道都补了 guard，只有这两个是 preload 引导阶段必需的同步调用、被漏掉了——典型的「大面积修复时漏掉入口」。

---

### E-P1-3 `sandbox: false` + 命令行 `--no-sandbox` + 全仓无 CSP：用沙箱换启动的代价没有在别处补回来

- `app/src/main.ts:31-32`
```ts
app.commandLine.appendSwitch("no-sandbox");
app.commandLine.appendSwitch("disable-gpu-sandbox");
```
- `app/src/main.ts:213-218`
```ts
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
```
- 命令行也带着：`app/build/launch.bat:21` `start "" "%~dp0AohuangShangpin.exe" --no-sandbox %*`；`app/build/installer.nsh:40/42` 创建快捷方式时写死 `'--no-sandbox'`。
- **CSP：三处都没有。** `web/index.html`（11 行全文）无 `Content-Security-Policy` meta；`web/dist/index.html` 无；`app/src/main.ts` 无 `onHeadersReceived`（`grep -n "onHeadersReceived\|webSecurity\|Content-Security-Policy\|setPermissionRequestHandler\|setPermissionCheckHandler\|setCertificateVerifyProc\|will-attach-webview" app/src/*.ts` → **全部无命中**）。

这不是「关沙箱是错的」——`app/src/main.ts:21-30` 与 `launch.bat:5-19` 记录了决定性实验（无参数启动连第一行 JS 都不执行、只加 `--no-sandbox` 就能到 ready），关沙箱是这台机器上**必须**的。问题是：**关掉 Chromium 沙箱后，唯一的纵深防线就是 CSP 和权限处理器，而这两者一个都没配。** 在 `file://` 页面下，任何一次 XSS（哪怕是商品标题里塞进来的富文本）都直接握有 `window.desktop.update.download` —— 同页同 frame，`trustedSender` 全过 —— 再叠加 E-P0-1 就是写任意路径。

**性质：跷跷板。** 关沙箱（A 的代价）没在 CSP/权限层面补回来（B），代价被推迟到最后一道防线。

---

### E-P1-4 `update:download` 成功后自动调用 `markAutoApply()` —— 用户没同意就会被静默升级，与产品决策直接冲突

- `app/src/main.ts:386-392`
```ts
  const result = await downloadInstaller(manifest, broadcastProgress);
  // 下载成功就打上「退出时自动安装」的标记。
  //
  // 2026-11-23 补：`update:set-auto-apply` 这个 IPC 在前端**从来没被调用过**
  // （`grep setAutoApply web/src` 为空），所以 `isAutoApplyMarked()` 永远是
  // false —— 「下好之后关软件自动静默升级 + 自动重启」这条负责人要求的
  // 路径实际上从来没有生效过，用户只能手动点「重启并更新」。
  if (result?.ok) await markAutoApply();
```
- 产品决策就在 5 个文件之外，且方向相反。`web/src/components/layout/UpdateDialog.tsx:102-108`
```ts
  // 只检查有没有已下好的包，**不自动下载、不自动安装**。
  //
  // 负责人 2026-11-23：「如果软件有新版本会自动弹出来说有新版本，但是用户
  // 可以选择更新或者取消两种，正常大厂软件不都是这样吗」。
  // 原来是弹出来就开始下、下完立刻装、装完重启 —— 用户正在处理商品时被
  // 强行重启，编辑中的东西全丢。现在弹窗只告知，点「立即更新」才动手。
```
- **两段注释都是「2026-11-23」，一天之内一条决策被另一条原地推翻**，且推翻的那条写在主进程（界面上看不到）。用户点「立即更新」下载完、关软件 → 静默安装 + 自动重启。`UpdateDialog` 的「取消」按钮只是 `onClose`（`UpdateDialog.tsx:34`），**无法撤销已经打上的 auto-apply 标记**（没有任何 UI 通道能 `clearAutoApplyMark`；`preload.ts:44-46` 暴露了 `setAutoApply`/`autoApplyStatus` 但 `web/src` 无调用者）。

**性质：补丁叠补丁。** 为了让一条从未生效的路径生效，把默认值从「不装」改成「装」，同时界面上没有任何开关能关掉它。

---

### E-P1-5 三套「清理安装包」机制互不知道对方存在，其中一套是死代码（真 PowerShell 已验证）

| 机制 | 位置 | 实际行为 |
|---|---|---|
| `pruneStaleInstallers()` | `updateInstaller.ts:706-724`，`main.ts:508` 每次启动 `void` 调用 | 只留 `pending.json` 那个，其余 `.exe`/`.part` 全删。**与下载无同步**，双开时删对方正在写的 `.part` |
| PowerShell 内联清理 | `updateDownload.ts:310` | **实测 0 个对象被删**（缺 `-Recurse` 与尾部通配符，`-Include` 静默匹配空集） |
| `discardInstaller()` | `updateInstaller.ts:683-694`；`main.ts:400-404` 挂 `update:discard` | 删 `pending.json` + updates 内所有 `.exe`/`.part`。`web/src` 无调用者 |

死代码证据（`updateDownload.ts:310` 原文 + 实测）：
```
"Get-ChildItem -Path (Split-Path -LiteralPath $setup) -Include *.exe,*.part -File | Where-Object { $_.FullName -ne $setup } | Remove-Item -Force",
```
```
--- Get-ChildItem -Include without -Recurse ---
0        ← a.exe 和 b.part 都在，匹配到 0 个
--- with trailing wildcard ---
2
```

后果：`updates/` 目录里会累积 77MB × N 的安装包（每次下载装完由 PowerShell 的 `Remove-Item -LiteralPath $setup` 删掉一个，但「之前几次的残留」永远不会被这条命令清）。表现在用户磁盘上，不表现在功能上，所以长期没被发现。

**性质：跷跷板。** 三套机制各自为「修某次事故」而加，没有收敛成一个。

---

### E-P1-6 `verify-desktop-package.mjs` 的 stale 检测只比版本号，不比 `dist/*.js` 与 `src/*.ts` 的对应 —— 已实测放过整整一个 Inno 时代的死文件进包

- **它检查的：** `scripts/verify-desktop-package.mjs:18-19`
```js
assert.equal(packaged.version, expected.version, "Packaged app version is stale");
assert.ok(fs.existsSync(path.join(unpacked, "AohuangShangpin.exe")), "Executable is missing");
```
- **它没检查的（实测证据）：**
```
app/dist/ 目录（--time-style=long-iso）
-rw-r--r--  3233  2026-11-23 10:41  installerArgs.js        ← src 里已无 installerArgs.ts
-rw-r--r--   781  2026-11-23 10:41  installerArgs.js.map
-rw-r--r-- 28801  2026-11-25 20:06  main.js                 ← 其余都是 11-25 重新编译的
```
`ls app/src/installerArgs.ts` → `No such file or directory`；`git ls-files app/src/` 也不含它。它被打进 asar 并随包分发（asar 条目列表）：
```
\dist
\dist\installerArgs.js          ← Inno 时代的 /VERYSILENT 逻辑
\dist\installerArgs.js.map
\dist\main.js
...
\package.json
```
- **它里面的内容确实是坏的：** `app/dist/installerArgs.js` 仍含 `INNO_SILENT_FLAGS = ["/VERYSILENT", ...]` 与 `"/DIR="` 引号逻辑。虽无任何模块 require 它（`main.js` / `updateDownload.js` / `updateInstaller.js` 内 `installerArgs` 均不出现），但 `verify-desktop-package.mjs:47` 的断言 `!codeLines(installerCode).includes("/VERYSILENT")` 只扫 `updateDownload.js`——**扫不到这个文件，所以绿灯。**

- **同类：`latest.yml` 停在 0.1.21。** `release/latest.yml`：
```yaml
version: 0.1.21
path: 奥皇上品-0.1.21-Setup.exe
releaseDate: '2026-11-14T11:18:15.000Z'
```
比当前 `0.1.54` 落后 33 个版本。仓库里没有任何 `electron-updater` 依赖（`app/package.json` 只有 devDependencies）、`electron-builder.yml` 无 `publish` 段，所以它是历史残留——但**没有任何检查会告诉你它过期了**。

- **同时被证明有效的部分（要记功）：** L65 逐字节比 8 个 `web/dist` 与 `resources/web`；L197-241 用 mock electron 在 VM 里真跑 `dist/main.js`，断言 `boot.apiBase === "https://aohuang888.com/miaoshou-platform"`、`boot.token === ""`、窗口尺寸、不可信页面读/写 session 均被拒、不可信导航被拦。

**性质：根因。** 校验口径覆盖了「版本号」，没覆盖「产物与源码一致」，而后者才是 stale 的真实形态。

---

### E-P1-7 `build.ps1` 不调用 `publish_release.py` / `verify_release.py` —— 2026-11-26 事故的闸门不在必经之路上

- `scripts/build.ps1` 全流程（53 行，五个步骤，无第六步）：
```
1/4 → python -X utf8 scripts\check_version.py       (L21)
    → pnpm check                                     (L23)
2/4 → pnpm build                                     (L28)
3/4 → pnpm exec electron-builder --win nsis          (L34)
4/4 → node scripts\verify-desktop-package.mjs        (L41)
    → 打印 SHA256                                    (L48)
```
- **全仓检索证实这两个脚本没有任何调用点：**
```
grep -rn "publish_release\|verify_release" package.json scripts/*.ps1 scripts/*.bat
→ (无命中，exit 1)
```
- 而 `publish_release.py` 自己把「必须跑」写得很重：`scripts/publish_release.py:26`
```
维护的 snippet 里 —— 所以发版时**必须**跑完本脚本，不能只上传文件。
```
`scripts/release_fixed_link.py:12-14` 描述了那条链的三条规则，其中第 3 条：
```
3. 跳转目标写在 `/etc/nginx/snippets/miaoshou-latest-redirect.conf`，
   由 `scripts/publish_release.py` 每次发版覆写，**绝不手改**。
```
- **这就是 2026-11-26 事故的同一形状：** 事故根因是「`verify_release.py` 只检查了带哈希 URL」——修正后加了固定链接检查；但**检查的执行依赖人记得敲第二条命令**。`build.ps1` 的成功不等于「用户下到的是新版」，两个脚本之间没有任何自动衔接。上次漏的是「检查项」，这次漏的是「检查项的触发器」。

**性质：根因（流程未收敛）。** 不是新 bug，是同一个根因换了个层面。

---

### E-P1-8 三处域名白名单三套标准：客户端 2 个域、服务端 4 个、渲染层 1 个

| 位置 | 白名单 | 后果 |
|---|---|---|
| `app/src/updateDownload.ts:10-17` | `aohuang888.com`、`aohuang888.xyz` | 无 `www.` |
| `server/core/constants.py:42-47` | 上述 + `www.aohuang888.com` + `www.aohuang888.xyz` | 更宽 |
| `web/src/lib/apiTarget.ts:21` | 仅 `aohuang888.com` | 最窄，`.xyz` 也拒 |

- `app/src/updateDownload.ts:64-69`
```ts
export function validateUpdateUrl(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:") return null;
    if (!ALLOWED_HOSTS.includes(url.hostname.toLowerCase())) return null;
```
- `server/core/constants.py:42-47`
```python
UPDATE_HOST_WHITELIST = {
    "aohuang888.com",
    "www.aohuang888.com",
    "aohuang888.xyz",
    "www.aohuang888.xyz",
}
```
- 后果不是安全问题（窄的更安全），是**功能静默失效**：服务端下发的清单若用 `www.aohuang888.xyz`（服务端自检通过），客户端 `validateUpdateUrl` 拒，界面只显示「更新地址不合法」；而三次检查用的**都是精确匹配**，没有任何一处会在写清单时做「这份清单能否被客户端接受」的反向验证。

**性质：补丁叠补丁。** `.xyz` 是 2026-11-20 为绕 CDN 加的，加的时候只改了需要改的地方，三处白名单没同步。

---

## P2 问题

### E-P2-1 `isCdnHeaders()` 是死代码，但 8 个单测和一份规则文档都在维护它

`app/src/updateDownload.ts:161`（声明）到 L176，生产链路零调用：`grep -rn "isCdnHeaders" app/src/ docs/` →
```
app/src/updateDownload.ts:161:export function isCdnHeaders(...)
app/src/updateDownload.test.mjs:184,192,198,207,212,220,226  ← 全是测试
docs/project/rules.md:637:所以 `app/src/updateDownload.ts` 的 `isCdnHeaders()` 决定用哪种：
docs/project/rules.md:657:第一版 `isCdnHeaders` 只按小写键取值：
```
`dist/updateDownload.js` 里出现 2 次（声明 + 导出），src 里 1 次——说明连编译产物都在维护它。而 `updateInstaller.ts:294-334` 的注释明确写着「**这个结论已经过时，那个函数已删除**」——删的是调用点，函数和测试留下了。29 个单测里 8 个在测一个没人调用的函数。**性质：补丁叠补丁。**

### E-P2-2 `validate_update_request()` 定义了从未被调用

`server/services/system_service.py:119-146` 定义了完整的 url/域名/sha256 校验，但全仓 `grep -rn "validate_update_request" server/` 只命中定义本身与 `.pyc`。真正的校验散在 `server/api/system.py` 内联（且 `url` 存在时才用白名单，否则信任清单）。**性质：补丁叠补丁。**

### E-P2-3 `docs/architecture.md` 四类能力声称与现实不符（本节汇总，前文已分别取证）

| 行 | 声称 | 实际 |
|---|---|---|
| L16 / L54 / L760 | 单实例锁 | 全仓无 `requestSingleInstanceLock`（见 E-P0-5） |
| L18 / L484 | `safeStorage` 落盘到 `userData/local-token.bin` | 无 `token.ts`；`sessionVault.ts` 落的是 `operator-session.v1.json`，且存的是用户会话不是本地 token |
| L19 | `electron-updater：检查 / 下载 / 静默安装` | 无 `updater.ts`，无 `electron-updater` 依赖，`latest.yml` 停在 0.1.21 |
| L20 | 优雅关停 `SIGTERM → 5s → kill` | `server?.kill()` + 立刻 `app.exit(0)`（见 E-P0-6） |
| L82 | `app/src/index.ts # app.whenReady、单实例锁、窗口` | 文件不存在，`whenReady` 在 `main.ts:505` |

**性质：文档债，但危害等级高于普通注释**——它是架构基线，下一个人会按它做决策。

### E-P2-4 未做代码签名 —— 上面所有「sha256 是唯一锚点」的问题被放大

```
Get-AuthenticodeSignature release\奥皇上品-0.1.54-Setup.exe  → Status=NotSigned
Get-AuthenticodeSignature release\win-unpacked\AohuangShangpin.exe → Status=NotSigned
```
`app/electron-builder.yml`（50 行全文）无 `certificateFile` / `signingHashAlgorithms` / `publisherName` / `win.sign` 任何配置。后果：SmartScreen 首启拦截、无发布者身份可核、`publish_release.py` 上传的包与任何人上传的同名包在操作系统层面无差别。**性质：根因（能力缺失）。**

### E-P2-5 `preload.ts` 暴露了 `runInstallerNow(installerPath)`，`web/src` 无调用者

`app/src/preload.ts:37-38`
```ts
    runInstallerNow: (installerPath: string) =>
      ipcRenderer.invoke("update:run-installer-now", installerPath),
```
同文件 L23-24 的注释声称「不暴露任意路径执行」，但这条 API 的签名就是吃路径。主进程侧有等值校验（`main.ts:406-412`）所以不致命，但「注释说的」和「接口做的」不一致。全仓唯一引用是类型声明 `web/src/types/global.d.ts:42`。**性质：补丁叠补丁。**

### E-P2-6 `resolveShortcutTarget()` 用裸 `powershell.exe`，与同文件 `powershellPath()` 的自相矛盾

`app/src/updateInstaller.ts:612-616`
```ts
    const output = execFileSync(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
      { encoding: "utf-8", timeout: 5000, windowsHide: true },
    ).trim();
```
而同文件 L402-412 有一个专门解决「PATH 被裁剪」的函数，注释写着「不能只写 `"powershell.exe"` 交给 PATH 解析」。这个函数的调用点在 `installOnQuit()` 里（`updateInstaller.ts:506`），**唯独 `resolveShortcutTarget` 没用**。失败后果：`catch { return ""; }` → 重启目标退回 `app.getPath("exe")` → 装到别的目录的机器上会启动一个不存在的路径。**性质：补丁叠补丁。**

### E-P2-7 `/D=` 参数对盘根目录的处理不完整

`app/src/updateDownload.ts:202-208` 的 `trimTrailingSeparators` 会去掉尾部 `\`，所以 `appDir = "D:\"` 变成 `"D:"`；`installerArguments` 生成 `/D=D:`。NSIS 对 `/D=D:` 的解释是「当前工作目录所在的 D: 盘路径」，而非「D: 盘根」。实际很难触发（`installDir()` 返回 `path.dirname(app.getPath("exe"))`，即 `D:\奥皇上品`），但契约上不完整。**性质：补丁叠补丁。**

### E-P2-8 `pruneStaleInstallers()` 在 `app.whenReady` 里 `void` 掉，与紧随其后的更新流无同步

`app/src/main.ts:508` `void pruneStaleInstallers();` —— 不 await。首次启动时若网络快、用户在 1 秒内触发下载，清理任务可能仍在遍历目录并删掉刚创建的 `.part`（`updateInstaller.ts:712-718` 的循环里每个文件独立 `await fs.rm`）。概率低但竞态真实存在。**性质：补丁叠补丁。**

### E-P2-9 仓库垃圾与工作区残留

- `app/src/installQuickCheck.txt`：内容 `placeholder`（5 字节，0 行），被 `git ls-files app/src/` 跟踪，没有任何代码读它。
- `scripts/__pycache__/`、`scripts/_audit_verify{,2,3,4}.py`、`scripts/fix_luoman.py`、`scripts/fix_converted_image_refs.py` 等一次性脚本留在 `scripts/` 根目录。
- `app/dist/installerArgs.js{,.map}`：源码已删、产物仍在（见 E-P1-6），`.gitignore:8` 的 `dist` 只管 git，不管 electron-builder 的 `files: [dist/**/*]`。
**性质：可维护性。**

---

## 更新链校验覆盖面清单

「软件内自动更新」与「用户手动下载」是两条独立链，必须分开看 —— 2026-11-26 事故正是「一条链全绿掩盖了另一条链全坏」。

| 入口 | 是否校验 | 校验什么 | 盲区 |
|---|---|---|---|
| ① `GET /system/version` → `fetch_manifest()`（服务端取清单） | 部分 | `ProxyHandler({})` 绕系统代理（`system_service.py:60`）；响应 256KB 上限 | **清单内容零校验**：`version` / `sha256` / `sizeBytes` 全原样透传；只对 `url` 做域名/协议/端口检查 |
| ② 服务端 `is_newer()` 判「有没有新版本」 | 是（弱） | `parse_version` 逐段取数字，遇无数字段 `break` | `"99..\..\..\pwn"` → `(99,)` → 判为更新（已实测）。**这是 E-P0-1 的过闸通道** |
| ③ 客户端 `update:download` 收渲染层 manifest | 部分 | `trustedSender` + `typeof === "object"`（`main.ts:377-380`） | 不校验 `version`（→ E-P0-1）；`sha256` 由渲染层提供（→ E-P0-2） |
| ④ `validateUpdateUrl()` | 是 | `https:`、精确匹配 2 个域、无 userinfo、端口空或 443、pathname 以 `.exe` 结尾（`updateDownload.ts:64-76`） | **不覆盖跳转后的最终域名**（`redirect: "follow"`）；`.exe` 只查后缀不查 query（`x.exe?y=a.apk` 通过，已实测） |
| ⑤ 分段下载 `fetchSegment()` | 是 | 必须 206；单片字节数 `=== segmentLength()`；`segmentsCoverExactly()` 覆盖精确 | 不校验 `Content-Range` 起点；不校验落盘位置（`file.write` 的返回值不管） |
| ⑥ 单流下载 `downloadSingleStream()` | 部分 | 必须 200；hash 从**网络流**计算 | **无 `out.on("error")`**：写失败被当成功、open 失败打死主进程（→ E-P0-3，两模式已复现）；hash 与落盘文件脱钩；背压丢弃 |
| ⑦ sha256 比对 | 是 | `hashHex !== expected` 即删 `.part` 并报错（`updateInstaller.ts:356`） | 分段路径 OK（hash 重读文件）；单流路径强度打折；**不比文件大小**（`fs.stat` 只回填 `size`） |
| ⑧ 原子改名 + 写 `pending.json` | 否 | 无 | `fs.rename` 失败就用随机后缀（`updateInstaller.ts:366-374`），路径从此不可预测；`pending.json` 记了 `sha256` 字段但**无消费者** |
| ⑨ `pendingInstaller()` 读 `pending.json` | **否** | 仅 `fs.access(data.path)` 存在性（`updateInstaller.ts:268`） | **下载到安装之间的本地替换/截断完全不校验**（→ E-P0-2）；不校验 path 是否在 `updates/` 内（→ E-P0-1） |
| ⑩ `update:install` → `quitAndInstall()` | 是（间接） | `trustedSender`；只走 pending | 见 ⑨ |
| ⑪ `update:run-installer-now` | 是 | `trustedSender` + 路径必须等于 `pending.path`（`main.ts:409-410`） | 仍以未校验内容的 `pending.path` 为锚；`web/src` 无调用者 |
| ⑫ `before-quit` → `installOnQuit()` | 是（间接） | pending 存在 + `isInstallRequested()` / `isAutoApplyMarked()` | `waitForLaunchMarker` 因 append-only 日志即时为真（→ E-P0-4）；`markAutoApply` 自动生效（→ E-P1-4） |
| ⑬ PowerShell 安装脚本 | 部分 | `-EncodedCommand`（UTF-16LE base64）避 CJK 乱码；`HasExited` 轮询 10 分钟；`Get-Process` 防双开 | `Get-ChildItem -Include` 清理命令实测 0 命中（死代码）；脚本本体是字符串拼接，无单测（`installPowerShellScript` 是纯函数但没有任何 `*.test.mjs` 调它） |
| ⑭ **固定链接 `latest-Setup.exe`（用户手动下载）** | 是（独立工具） | `verify_release.py`：比 sha256 + size；主域不匹配→阻塞，`.xyz`→仅 warn（除非 `--strict-cdn`） | **`build.ps1` 不调用它**（→ E-P1-7）；`.xyz` 旧缓存默认不阻塞；用户浏览器仍可能拿到旧包 |
| ⑮ `publish_release.py` 生成 snippet | 是 | `release_fixed_link.py:93-114` `fixed_link_is_safe()`：禁指向 `latest-Setup.exe` 自身、必须带哈希名、sha256 长度 64 | 纯手工步骤，无 CI/构建链强制 |
| ⑯ `verify-desktop-package.mjs` | 是（强） | asar version == `app/package.json`；exe 存在；无 `client-token.json`；无 `resources/server`；`/S` + `/D=`；无 `/VERYSILENT`；无硬编码 `'/S'`；无 `await discardInstaller()`；8 个 web 文件逐字节；VM 跑 `main.js` 断言 apiBase/token/窗口尺寸/会话 guard/导航拦截 | **不比 `dist/*.js` 与 `src/*.ts` 的对应**（→ E-P1-6）；不测 `validateUpdateUrl`；不测路径穿越；不测 `latest.yml`；不测代码签名 |
| ⑰ `check_version.py` | 是 | 四处版本号相等（root/`web`/`app` 的 `package.json` + `constants.py` 的 AST 提取） | 只比版本号，不比产物；本次实测通过（四处 0.1.54） |

**一句话结论：** 「① 服务端 → ② 版本比较 → ③ 渲染层 → ④ URL 白名单 → ⑤⑥ 下载 → ⑦ hash → ⑧ 落盘 → ⑨ 安装」这九环里，**⑨ 是全链唯一的零校验环**，而它正好是「决定执行哪个文件」的那一环；⑭ 是另一条链上唯一能挡住事故的环，而它不在构建链上。

---

## IPC 通道清单

主进程共注册 **17 个**通道（`grep -n "ipcMain.handle(\|ipcMain.on(" app/src/main.ts` 全量），preload 暴露 **17 个**（`preload.ts` 全量对应）。「入参校验」列列出的是**主进程侧**实际做的检查。

| 通道名 | 入参校验 | 风险 |
|---|---|---|
| `app:boot`（handle, L327） | **无**（无 `trustedSender`，无入参） | 低。返回 `bootInfo`：打包态 `token=""`，仅泄露 `apiBase`/`version`/`platform`；但 `AOHUANG_LOCAL_BACKEND=1` 时 `token` 为后端 `secret.key` → **E-P1-2** |
| `app:boot-sync`（on, L328） | **无**（同步 IPC，preload 引导用） | 同上。`sendSync` 阻塞渲染线程，且是唯一被 preload 在模块顶层调用的通道 |
| `session:read`（on, L336） | `sender === mainWindow.webContents` + `senderFrame === mainFrame` + `isTrustedPage(url)` | 无（三重 guard，`verify-desktop-package.mjs:229` 有断言） |
| `session:write`（on, L341） | 上述三重 + `value` 必为 `null` 或 `string.length ≤ 8192` | 无（同上，L232 有断言） |
| `window:minimize`（handle, L351） | **无** | 极低（纯 UI）。无 guard 意味着任意 frame 都能最小化窗口 |
| `window:toggle-maximize`（handle, L352） | **无** | 同上 |
| `window:close`（handle, L358） | **无** | 低。可触发 `window-all-closed` → `app.quit()` → 触发安装流程；但安装流程本身还需要 pending + 标记 |
| `window:is-maximized`（handle, L359） | **无** | 极低（只读） |
| `update:pending`（handle, L371） | `trustedSender` | 低。泄露待安装包的**绝对路径**与版本号 |
| `update:download`（handle, L376） | `trustedSender` + `typeof manifest === "object"` → `validateUpdateUrl` + sha256 正则 | **P0**。`version` 零校验 → 路径穿越（E-P0-1）；`sha256` 来自渲染层（E-P0-2）；成功后自动 `markAutoApply`（E-P1-4） |
| `update:install`（handle, L395） | `trustedSender` | 中。触发 `app.quit()` → `before-quit` 安装流；锚点是未校验内容的 `pending.json` |
| `update:discard`（handle, L400） | `trustedSender` | 低。删 `updates/` 内 `.exe`/`.part` + `pending.json`；无 `web/src` 调用者 |
| `update:run-installer-now`（handle, L406） | `trustedSender` + `path.resolve(pending.path) === path.resolve(入参)` | 中。等值校验挡住了「任意路径执行」，但仍信任 `pending.path`；无 `web/src` 调用者 |
| `update:set-auto-apply`（handle, L418） | `trustedSender` + `Boolean(enabled)`（隐式） | 低。无 `web/src` 调用者，所以用户无法通过界面关闭 auto-apply（E-P1-4） |
| `update:auto-apply-status`（handle, L425） | `trustedSender` | 低（只读） |
| `clipboard:copy-image`（handle, L480） | `trustedSender` + `http:`/`https:` 协议 + 响应 ≤ 32MB | **中**。`redirect: "follow"` → 白名单只覆盖第一跳，可导向任意域/内网地址并把字节塞进剪贴板（E-P1-1）；无内容类型校验（只靠 `nativeImage.createFromBuffer` 的 `isEmpty()` 兜底） |
| `clipboard:copy-text`（handle, L495） | `trustedSender` + `String(text ?? "")` | 低（`String()` 已归一化，无长度上限——但写入系统剪贴板无实际危害） |

**不存在「任意命令执行」通道。** 17 个通道里没有 `shell.openPath`、没有 `child_process` 直通、没有任意文件读写。`shell.openExternal` 只通过 `openWebLink()`（`main.ts:189-194`）触达，且只放行 `https:` / `http:`：

```ts
function openWebLink(value: string): void {
  try {
    const url = new URL(value);
    if (url.protocol === "https:" || url.protocol === "http:") void shell.openExternal(value);
  } catch { /* Ignore malformed external links. */ }
}
```
这一步是**对的**（`javascript:` / `file:` / `ms-msdt:` 之类全被挡），`setWindowOpenHandler` 与 `will-navigate` 的路径也正确（`main.ts:249-262`）：
```ts
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    openWebLink(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!isTrustedPage(url)) {
      event.preventDefault();
      openWebLink(url);
    }
  });
  mainWindow.webContents.on("will-redirect", (event, url) => {
    if (!isTrustedPage(url)) event.preventDefault();
  });
```

**唯一的协议处理缺口在 `isTrustedPage` 的 query 容忍度**（已实测，危害低）：`fileURLToPath()` 会剥掉 query，所以 `file:///.../web/index.html?apiBase=whatever` 仍被判为可信页。`will-redirect` 分支因此允许「同文件不同 query」的跳转——但目标文件仍是 `index.html`，没有实质危害。记录在此以备后续参考。

---

## 建议的回归测试

以下每条都对应上面一个具体结论，按「能立刻写、能挡住已发生的 bug」排序。前 6 条是 P0 的守门测试。

### 针对 E-P0-1（`version` 路径穿越）

1. **`version` 形状闸门单测**（`app/src/updateDownload.test.mjs` 里加，纯函数化 `downloadInstaller` 的路径计算部分）：
   ```
   输入 "..\\..\\..\\..\\..\\..\\Roaming\\...\\Startup\\aohuang" → 必须被拒
   输入 "99..\\..\\..\\pwn"            → 必须被拒
   输入 "0.1.55" / "1.0.0-beta.1"     → 必须通过（正向用例，防止过窄）
   输入 "C:\\Windows\\Temp\\x"        → 必须被拒
   输入 "\\\\server\\share\\x"        → 必须被拒
   ```
2. **`pendingInstaller()` 的路径包含断言**：伪造一个 `pending.json`，`path` 指向 `updates/` 之外 → 必须返回 `null`。
3. **端到端（本地 mock HTTP）**：起一个本地 https mock 提供 `version.json`（`version` 为穿越串、`sha256` 为真实值），走 `update:download` IPC → 断言 `updates/` 之外的路径**没有任何新文件**，且返回 `{ok: false}`。

### 针对 E-P0-2（完整性锚点）

4. **安装前重校验**：下好一个真包 → 手工改掉 `pending.json` 指向的 exe 的最后一个字节 → 关软件 → 断言 PowerShell **没有**被执行（`install.log` 无新 `launching:`、安装包仍在原地、返回错误而不是静默）。
5. **`sha256` 与渲染层解耦**：mock 后端 `/system/version` 返回 sha256=A，渲染层传 sha256=B → 断言主进程以 A 为准或直接拒绝。

### 针对 E-P0-3（单流写流错误）

6. **写入阶段失败必须可感知**：`out.destroy(new Error("ENOSPC"))` 后再 `end()`（形状同 `_build_tmp/audit_e_writestream2.mjs` 场景 A）→ 断言 `downloadSingleStream` 返回 `{ok: false}`，**而不是** `{ok: true, sha256: <完整内容的 hash>}`。当前实现返回后者，这条测试现在必然红（探针已证实 `endCallbackFired=true`）。
7. **打开阶段失败不得打死进程**：`partialPath` 指向不存在的父目录（形状同 `_build_tmp/audit_e_writestream2.mjs` 场景 B）→ 断言函数返回 `{ok: false}` 且**进程存活**。当前实现触发 unhandled `'error'` 使 Node 退出码 1，这条测试现在必然红。**这是本报告里唯一一条「跑测试就等于打崩溃」的用例，注意在子进程里跑。**
8. **落盘大小比对**：`fs.stat(finalPath).size !== (total || received)` → 必须拒绝并删 `.part`（现有代码只在 `stat` 时回填 `size`）。
9. **单流 hash 绑定落盘内容**：与分段路径对齐——同一个「网络层数据完整、落盘残缺」场景，两条路径的返回值必须一致（都失败）。当前分段路径失败、单流路径成功，这条测试现在必然红。
10. **合成回归（E-P0-1 × E-P0-3）**：`manifest.version` 带层级穿越 + 合法 `sha256` → 断言**既**没有在 `updates/` 外产生文件，**也**没有让进程崩溃退出。

### 针对 E-P0-4（`waitForLaunchMarker` 陈旧日志）

11. **历史日志不污染判据**：预置一份含 10 条 `launching:` 的 `install.log` → 调 `waitForLaunchMarker(logPath, 1000)` → 断言返回 **false**（当前实现会返回 true，这条测试现在必然红）。
12. **增量判据正向**：预置历史日志 → 启动一个会写新 `launching:` 的进程 → 断言在 1 秒内返回 true，且 15 秒超时分支不被触发。

### 针对 E-P0-5（单实例）与 E-P0-6（孤儿 sidecar）

13. **单实例**：连开两次 `AohuangShangpin.exe` → 断言系统进程里 `AohuangShangpin.exe` 只有 1 个、`pending.json` 只被一个实例持有、第二次启动**没有**触发 `pruneStaleInstallers`（用 `install.log` 或临时打点验证）。
14. **孤儿 sidecar**：`AOHUANG_LOCAL_BACKEND=1` 启动 → 记下 `aohuang-server.exe` 的 PID → `taskkill /F /PID <electron主进程>` → 等 10 秒 → 断言该 PID **已不存在**（当前实现必然红）。
15. **端口复用不静默串台**：手工起一个占住 8812 的假后端（返回 200 的 `/api/v1/system/health`）→ 启动客户端 → 断言它**没有**连到那个假后端（例如要求 health 响应带实例 ID/PID 字段并比对）。

### 针对 E-P1-1 / E-P1-2 / E-P1-3（边界）

16. **`validateUpdateUrl` 跳转不被跟随**：本地起 302 服务（白名单域 → 127.0.0.1）→ 断言 `fetchSegment` / `fetchImageBuffer` **不**请求第二跳，或第二跳被重新校验并拒绝。可参照 `server/services/update_check.py::_stream` 的现有测试写法。
17. **`validateUpdateUrl` 表驱动**（把已实测的结论固化）：放行 `https://AOHUANG888.COM/x.exe`、`https://aohuang888.com:443/x.exe`、`x.EXE`；拒绝端口 8443、userinfo、`aohuang888.com.evil.com`、`evilaohuang888.xyz`、尾部点 `aohuang888.xyz.`、西里尔 `аohuang888.com`、`http:`、`file:`、非 `.exe`。现有测试已覆盖大部分，**缺「尾部点」与「同形字」两个用例**。
18. **`app:boot` / `app:boot-sync` 的 guard**：扩展 `verify-desktop-package.mjs` 的 VM 断言——用一个非主窗口的 mock `event` 调这两个通道，断言返回 `null`/空（当前会返回完整 `bootInfo`，这条测试现在必然红）。
19. **CSP 存在性断言**：在 `verify-desktop-package.mjs` 里加 `assert.ok(/Content-Security-Policy/.test(indexHtml))`。注意：加 CSP 需要先确认 `file://` 下的 `script-src` 不影响 Vite 产物加载，属于需要真机验证的改动，测试可以先以「已知欠账」形式记录（`test.todo`）而不是立即变红。

### 针对 E-P1-4 / E-P1-5 / E-P1-6 / E-P1-7（流程与产物）

20. **auto-apply 必须显式**：`update:download` 成功后断言 `isAutoApplyMarked() === false`（除非用户此前通过 `update:set-auto-apply(true)` 显式开启）。当前实现必然红。
21. **取消能撤销**：点「取消」→ 断言 `isAutoApplyMarked() === false`，且 `pending.json` 被清或保留但不再自动执行。
22. **PowerShell 清理命令有效性**：把 `installPowerShellScript()` 生成的真脚本在临时目录跑一遍（目录里放 `a.exe` + `b.part` + `pending.json` + 目标 `$setup`），断言 **除 `$setup` 外全部被删**。当前实现必然红（`-Include` 0 命中）。
23. **产物与源码一致性**：`verify-desktop-package.mjs` 加一条——遍历 `app/dist/*.js`，断言每个文件都有对应的 `app/src/*.ts`（或 `.mjs`）源文件；反查 `app/src` 里每个 `.ts` 都出现在 `dist`。当前会因 `dist/installerArgs.js` 变红。
24. **`build.ps1` 串上固定链接校验**：在 `scripts/build.ps1` 的第 4 步之后加一步调用 `verify_release.py --strict-cdn`（或至少打印「下一步必须跑 `publish_release.py`」并检查 `release/` 里是否有本次版本的 `version.json` 草案）。这条不是测试是流程修复，但需要一个**回归断言**：在 `verify-desktop-package.mjs` 里断言「`build.ps1` 的步骤数 ≥ 5 且包含 `verify_release`」——用脚本守住脚本。
25. **`latest.yml` 陈旧告警**：`verify-desktop-package.mjs` 比对 `release/latest.yml` 的 `version` 与 `app/package.json` 的 `version`，不一致就 warn（不 fail，因为它本来就没被消费）。当前会立刻告警：0.1.21 vs 0.1.54。
26. **`installPowerShellScript` 纯函数单测**：它是纯函数（`updateDownload.ts:272`），却没有任何 `*.test.mjs` 调它。补上——断言生成的脚本能被 `powershell.exe -EncodedCommand` 正常解析（`$ErrorActionPreference` 不报语法错），且 `/D=` 出现在 `/S` 之后且不带引号。

### 针对 E-P1-8 / E-P2（一致性）

27. **三处白名单一致性**：一条测试同时读 `app/src/updateDownload.ts` 的 `ALLOWED_HOSTS`、`server/core/constants.py` 的 `UPDATE_HOST_WHITELIST`、`web/src/lib/apiTarget.ts` 的字面量，断言集合关系明确（客户端 ⊆ 服务端，且渲染层的窄集合是有意为之要写进注释）。任一被单独改动时测试变红。
28. **`isCdnHeaders` 死代码检测**：断言 `updateDownload.ts` 里每个 `export function` 都至少有一个非测试的调用点。这条会同时抓到 `isCdnHeaders` 与 `runInstallerNow` 的暴露。
29. **`app.asar` 内容白名单**：断言 asar 条目集合恰好等于期望列表（当前应含 5 个 `.js` + 5 个 `.js.map` + `package.json`，共 14 项含目录）。删除 `installerArgs.js` 后这条会把 16 项 / 14 项的差异暴露出来。

---

**收口：本次审计未修改任何源码。** 全部证据可复现，探针脚本都在 `_build_tmp/`（`.gitignore:67` 已忽略）：

| 结论 | 复现方式 | 产物 |
|---|---|---|
| E-P0-1 路径穿越 | `node _build_tmp/audit_e_traversal3.mjs`（`path.join` 参数与 `updateInstaller.ts:299` 逐字一致） | `_build_tmp/audit_e_traversal{,2,3}.mjs` |
| E-P0-1 `is_newer` 过闸 | `python -X utf8 -c "... parse_version(c), is_newer(c,'0.1.54')"` → `'99..\..\pwn' → (99,) → True` | 直接调用 `server/services/system_service.py` 的真函数 |
| E-P0-3 写流两种失败模式 | `node _build_tmp/audit_e_writestream2.mjs` → 模式 A `endCallbackFired=true`；模式 B `[exit=1]` unhandled error | `_build_tmp/audit_e_writestream{,2}.mjs` |
| E-P1-5 `-Include` 死代码 | 真 PowerShell 临时目录放 `a.exe`+`b.part` → `-Include` 无 `-Recurse` 匹配 0，带尾部 `*` 匹配 2 | 内联命令行 |
| `/D=` 参数不被加引号 | `Start-Process -ArgumentList @('/S','/D=C:\Program Files\Ao Huang')` → 探测脚本回显 `ARGS=[/S /D=C:\Program Files\Ao Huang]`（未加引号，符合 NSIS 要求） | `_build_tmp/audit_e_args_probe.ps1` |
| E-P2-4 签名 | `Get-AuthenticodeSignature` → 安装包与 `win-unpacked\AohuangShangpin.exe` 均 `Status=NotSigned` | 内联 |
| E-P1-6 asar 内容 | `@electron/asar` 读 `release/win-unpacked/resources/app.asar`：14 条目、含 `dist/installerArgs.js`；`package.json` 仅 4 字段 | 内联 `node -e` |
| E-P0-5 无单实例 | 全仓 grep + asar 内 `dist/main.js` 的 `requestSingleInstanceLock` → 均 false | 内联 |

清理建议：`_build_tmp/audit_e_*.mjs`、`_build_tmp/audit_e_args_probe.ps1` 可随时删除，报告里已完整记录复现方式与输出。
