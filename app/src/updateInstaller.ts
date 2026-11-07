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
    // 校验通过才原子改名：这之前任何失败都不会留下「看起来已下载」的文件
    await fs.rm(target, { force: true });
    await fs.rename(partial, target);
    const stat = await fs.stat(target);
    await fs.writeFile(
      path.join(dir, "pending.json"),
      JSON.stringify({ path: target, version: manifest.version, sha256: actual,
                       downloadedAt: Date.now() }),
      "utf-8",
    );
    return {
      ok: true,
      path: target,
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
 * Inno Setup 的 `/S` 是静默安装。这里**不等待**安装器退出：
 * 它会关掉本程序，等待会让主进程挂在一个正在被卸载的进程上。
 * 独立进程 + stdio ignore，保证安装器不被本进程的退出带走。
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
 * 为什么不在 `before-quit` 里无条件装：用户可能只是关掉程序去干别的，
 * 强制安装会让他下次打开发现版本变了却不知道为什么。所以只有显式点
 * 「重启并安装」才装。
 */
export async function installOnQuit(): Promise<void> {
  const pending = await pendingInstaller();
  if (!pending) return;
  launchInstaller(pending.path, true);
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

/** 清掉已下载的安装包（用户反悔 / 版本已是最新）。 */
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

/** 广播下载进度给所有窗口（渲染层用 preload 暴露的 onProgress 订阅）。 */
export function broadcastProgress(progress: DownloadProgress): void {
  for (const window of BrowserWindow.getAllWindows()) {
    window.webContents.send("update:progress", progress);
  }
}
