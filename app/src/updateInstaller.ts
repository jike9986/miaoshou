/**
 * 客户端自更新：下载 → 校验 → 退出时静默安装。
 *
 * 为什么放在主进程而不是走后端 `/system/update`：
 *   正式客户端连的是中心服务（aohuang888.com），后端 `shared_mode=True`，
 *   `POST /system/update` 会直接回 `accepted:false / reason:"shared-mode"`
 *   —— 共享服务器上下载几十兆安装包没意义，真正要装的是操作者这台机器。
 *   所以下载、校验、安装必须在客户端本地做。
 *
 * 三条硬约束（沿用后端 update_check.py 的结论）：
 *   1. 只下 https、只下 aohuang888.com。更新地址来自远端清单，
 *      一旦清单被改写就是「替攻击者下载任意 exe」。
 *   2. 边下边算 sha256，不一致立刻删掉，绝不交给安装器。
 *   3. 先写 .part 再原子 rename。几十兆的文件断电/崩溃会留半截 exe，
 *      下一次被当成「已下载」交给安装器 —— 用户会看到安装器报损坏。
 *
 * 安装时机：**退出时**装，不在下载完立刻装。
 *   立刻装会杀掉当前进程，正在跑的发布/认领任务全丢。
 *   下载完只提示「重启后自动安装」，用户自己决定什么时候重启。
 */
import { createWriteStream } from "node:fs";
import fs from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { spawn } from "node:child_process";
import { app, BrowserWindow, net } from "electron";

/** 只允许从官方域下载；与 web/src/lib/apiTarget.ts 的 officialUpdateUrl 保持一致。 */
const ALLOWED_HOST = "aohuang888.com";

export interface UpdateManifest {
  version: string;
  url: string;
  sha256: string;
  sizeBytes?: number;
  notes?: string;
}

export interface DownloadProgress {
  received: number;
  total: number;
  percent: number;
}

export interface DownloadResult {
  ok: boolean;
  path?: string;
  version?: string;
  size?: number;
  sha256?: string;
  error?: string;
}

function updatesDir(): string {
  return path.join(app.getPath("userData"), "updates");
}

/** 校验下载地址：必须 https + 官方域 + .exe，且不带账号密码或非常规端口。 */
export function validateUpdateUrl(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:") return null;
    if (url.hostname !== ALLOWED_HOST) return null;
    if (url.username || url.password) return null;
    if (url.port && url.port !== "443") return null;
    if (!url.pathname.toLowerCase().endsWith(".exe")) return null;
    return url.href;
  } catch {
    return null;
  }
}

/** 已下载但尚未安装的安装包路径（供启动时提示与退出时安装）。 */
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

/**
 * 下载并校验安装包。
 *
 * `onProgress` 每收到一块就回调一次，渲染层据此画进度条；
 * 下载失败一律清掉 .part，不留半截文件。
 */
export async function downloadInstaller(
  manifest: UpdateManifest,
  onProgress?: (progress: DownloadProgress) => void,
): Promise<DownloadResult> {
  const safeUrl = validateUpdateUrl(manifest.url);
  if (!safeUrl) return { ok: false, error: "更新地址不合法（只允许官方 https 地址）" };
  const expected = String(manifest.sha256 || "").trim().toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(expected)) {
    return { ok: false, error: "更新清单缺少有效的 sha256，拒绝下载" };
  }

  const dir = updatesDir();
  await fs.mkdir(dir, { recursive: true });
  const target = path.join(dir, `AohuangShangpin-${manifest.version}-Setup.exe`);
  const partial = `${target}.part`;

  try {
    await fs.rm(partial, { force: true });
    const total = Number(manifest.sizeBytes) || 0;
    let received = 0;
    const hash = createHash("sha256");

    await new Promise<void>((resolve, reject) => {
      const request = net.request({ url: safeUrl, redirect: "follow" });
      request.on("response", (response) => {
        if (response.statusCode !== 200) {
          reject(new Error(`下载失败（HTTP ${response.statusCode}）`));
          return;
        }
        const out = createWriteStream(partial);
        response.on("data", (chunk: Buffer) => {
          received += chunk.length;
          hash.update(chunk);
          out.write(chunk);
          if (onProgress) {
            const denominator = total > 0 ? total : received;
            onProgress({
              received,
              total,
              percent: denominator > 0
                ? Math.min(100, Math.round((received / denominator) * 100))
                : 0,
            });
          }
        });
        response.on("end", () => out.end(() => resolve()));
        response.on("error", (error: Error) => {
          out.destroy();
          reject(error);
        });
      });
      request.on("error", reject);
      request.end();
    });

    const actual = hash.digest("hex");
    if (actual !== expected) {
      await fs.rm(partial, { force: true });
      return {
        ok: false,
        error: `安装包校验失败（期望 ${expected.slice(0, 12)}… 实际 ${actual.slice(0, 12)}…）`,
      };
    }
    // 校验通过才原子改名。目标文件可能被系统锁住（上次下载的 exe 还在），
    // 先尝试删旧文件，EBUSY 时改用随机后缀名绕开（不影响安装逻辑：
    // pending.json 记的是实际路径，launchInstaller 从那里读）。
    let finalPath = target;
    try {
      await fs.rm(target, { force: true });
      await fs.rename(partial, target);
    } catch {
      // 如果目标文件被锁无法删/改名，就用带随机后缀的临时名
      const rand = Math.random().toString(36).slice(2, 8);
      finalPath = `${target}.${rand}.exe`;
      await fs.rename(partial, finalPath);
    }
    const stat = await fs.stat(finalPath);
    await fs.writeFile(
      path.join(dir, "pending.json"),
      JSON.stringify({ path: finalPath, version: manifest.version, sha256: actual,
                       downloadedAt: Date.now() }),
      "utf-8",
    );
    return {
      ok: true,
      path: finalPath,
      version: manifest.version,
      size: stat.size,
      sha256: actual,
    };
  } catch (error) {
    await fs.rm(partial, { force: true }).catch(() => {});
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * 启动安装器。
 *
 * NSIS /S 静默安装，安装完成后自动启动程序（通过 /postinstall 参数触发
 * installer.nsh 里的 customInstallEnd，或直接在安装器退出后由我们 spawn 软件）。
 * 这里**不等待**安装器退出：它会关掉本程序，等待会让主进程挂在一个正在被卸载的进程上。
 */
export function launchInstaller(installerPath: string, silent = true): { ok: boolean; error?: string } {
  try {
    const args = silent ? ["/S"] : [];
    const child = spawn(installerPath, args, {
      detached: true,
      stdio: "ignore",
      windowsHide: true,
    });
    child.unref();
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * 退出时安装：仅当有 pending.json 且用户已同意（`quitAndInstall` 被调用）。
 *
 * 安装完成后自动重启软件：写一个 .cmd 脚本到 temp 目录，用 cmd.exe 运行它。
 * PowerShell Start-Process 在包含中文的路径上有 GBK 编码问题，cmd /c 没有。
 */
export async function installOnQuit(): Promise<void> {
  const pending = await pendingInstaller();
  if (!pending) return;
  const exePath = app.getPath("exe");
  // 写 .cmd 脚本：等 1s（让本进程先退出）→ 静默安装 → 等 2s → 启动新版
  // 用 gbk 编码：cmd.exe 在中文 Windows 上按 GBK 读脚本文件，
  // 如果用 utf-8 写，中文路径（如 D:\软件\奥皇上品）会变成乱码导致找不到文件。
  // 用 PowerShell -EncodedCommand 避免中文路径编码问题：
  // Base64-encode UTF-16LE 命令字符串，PowerShell 解码后是 Unicode，不受控制台代码页影响。
  const psCommand = [
    `Start-Sleep -Milliseconds 1500`,
    `Start-Process -FilePath ([System.Text.Encoding]::Unicode.GetString([System.Convert]::FromBase64String('${Buffer.from(pending.path, "utf16le").toString("base64")}'))) -ArgumentList '/S' -Wait`,
    `Start-Sleep -Seconds 2`,
    `Start-Process -FilePath ([System.Text.Encoding]::Unicode.GetString([System.Convert]::FromBase64String('${Buffer.from(exePath, "utf16le").toString("base64")}'))) -ArgumentList '--no-sandbox'`,
  ].join("; ");
  const encoded = Buffer.from(psCommand, "utf16le").toString("base64");
  const { spawn: _spawn } = await import("node:child_process");
  _spawn(
    "powershell.exe",
    ["-WindowStyle", "Hidden", "-NonInteractive", "-EncodedCommand", encoded],
    { detached: true, stdio: "ignore" }
  ).unref();
  await discardInstaller();
}

/** 给渲染层用的「重启并安装」：先记下意图，再退出，由 before-quit 执行安装。 */
export async function quitAndInstall(): Promise<{ ok: boolean; error?: string }> {
  const pending = await pendingInstaller();
  if (!pending) return { ok: false, error: "没有已下载的安装包" };
  installRequested = true;
  // 给渲染层回包的时间，再触发退出
  setTimeout(() => app.quit(), 200);
  return { ok: true };
}

let installRequested = false;

export function isInstallRequested(): boolean {
  return installRequested;
}

/** 清掉已下载的安装包（用户反悔 / 版本已是最新 / 安装已启动）。 */
export async function discardInstaller(): Promise<void> {
  try {
    const dir = updatesDir();
    await fs.rm(path.join(dir, "pending.json"), { force: true });
    const entries = await fs.readdir(dir).catch(() => [] as string[]);
    for (const name of entries) {
      if (name.toLowerCase().endsWith(".exe") || name.toLowerCase().endsWith(".part")) {
        await fs.rm(path.join(dir, name), { force: true });
      }
    }
  } catch { /* 清理失败不影响主流程 */ }
}

/**
 * 启动时清理「不是当前待装版本」的历史安装包。
 *
 * 为什么需要：每次软件内更新都会把安装包下到 userData\updates\，
 * 安装器只负责装到程序目录，管不到 userData —— 不清理就会每更新一次
 * 多留一个 77MB 的 exe。更新五次就是 385MB 垃圾。
 *
 * 保留当前 pending.json 指向的那一份（用户可能还没点「重启并安装」），
 * 其余 exe / part 一律删掉。
 */
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
    // pending.json 指向的文件已经没了（用户手动删了 exe），标记也清掉
    if (!pending && entries.includes("pending.json")) {
      await fs.rm(path.join(dir, "pending.json"), { force: true }).catch(() => {});
    }
  } catch { /* 清理失败不影响启动 */ }
}

/** 广播下载进度给所有窗口（渲染层用 preload 暴露的 onProgress 订阅）。 */
export function broadcastProgress(progress: DownloadProgress): void {
  for (const window of BrowserWindow.getAllWindows()) {
    window.webContents.send("update:progress", progress);
  }
}
