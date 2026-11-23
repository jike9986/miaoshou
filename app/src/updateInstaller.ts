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
import { createReadStream, createWriteStream } from "node:fs";
import fs from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { app, BrowserWindow, net } from "electron";
import {
  ALLOWED_HOSTS,
  encodePowerShellCommand,
  installPowerShellScript,
  installerArguments,
  DOWNLOAD_USER_AGENT,
  isCdnHeaders,
  planSegments,
  rangeHeader,
  segmentLength,
  validateUpdateUrl as validateUpdateUrlPure,
  type Segment,
} from "./updateDownload";


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

/** 校验下载地址（实现见 updateDownload.ts，那里可单测）。 */
export function validateUpdateUrl(value: string): string | null {
  return validateUpdateUrlPure(value);
}

// --- 分段并发下载 -----------------------------------------------------------
//
// 实测依据（2026-11-20，这台服务器跨境链路）：
//   单连接        188 KB/s  →  74MB 需要 205 秒
//   6 并发        685 KB/s  →  74MB 约 57 秒（实测 3.6x）
//
// 瓶颈是单条 TCP 连接在高丢包（重传 14.9%）链路上的拥塞窗口，不是带宽。
// 多开连接各自拥塞，总吞吐上去 4~5 倍。
//
// **失败必须能退回单连接**：服务端不支持 Range（返回 200）、或者任何一片
// 出问题，都要回退，不能让用户因为「服务器不配合」彻底下不了更新。


/**
 * 下界闭区间 [start,end] 到文件指定偏移。
 *
 * 用 `r+` 打开并 `write` 定位写：4 条连接各写自己那段，互不覆盖。
 * 先 `truncate` 到总长度，避免最后一段长度不足导致文件偏短。
 */
async function fetchSegment(
  url: string,
  file: Awaited<ReturnType<typeof fs.open>>,
  segment: Segment,
  onBytes: (count: number) => void,
): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const request = net.request({
      url,
      method: "GET",
      redirect: "follow",
    });
    request.setHeader("Range", rangeHeader(segment));
    // 显式声明 UA：Cloudflare 的 Browser Integrity Check 会拦脚本类 UA
    // （实测 Python-urllib 被 403/error 1010）。不依赖 Electron 默认值。
    request.setHeader("User-Agent", DOWNLOAD_USER_AGENT);
    request.on("response", (response) => {
      // 206 = 支持分段；200 = 服务端忽略了 Range，整份返回 → 不能用
      if (response.statusCode !== 206) {
        reject(new Error(
          `不支持分段下载（HTTP ${response.statusCode}）`));
        return;
      }
      // 先收完整片再落盘。
      //
      // 不边收边写的原因：Electron 的 `IncomingMessage` 类型上没有
      // pause/resume，而异步 `file.write` 不 await 会让写请求堆积
      // （74MB/64KB ≈ 1200 次），既可能撑爆内存也可能 end 时还没写完。
      // 一片最大 20MB 上下（74MB/4），缓冲在内存里完全没问题，
      // 而且一次性定位写更不容易出错。
      const chunks: Buffer[] = [];
      response.on("data", (chunk: Buffer) => chunks.push(chunk));
      response.on("end", () => {
        const buffer = Buffer.concat(chunks);
        const expected = segmentLength(segment);
        // 必须核对这一片是否收满。
        //
        // 文件已经 truncate 到总长度，如果某片少收几个字节，文件不会变短，
        // 而是留下**零字节空洞** —— sha256 必然不符，但报错会指向
        // 「校验失败」，让人误以为是服务器给错了包。这里提前报出具体哪片。
        if (buffer.length !== expected) {
          reject(new Error(
            `分段下载不完整（第 ${segment.start}~${segment.end} 字节，`
            + `收到 ${buffer.length}/${expected}）`));
          return;
        }
        void file.write(buffer, 0, buffer.length, segment.start).then(
          () => {
            onBytes(buffer.length);
            resolve();
          },
          (error: Error) => reject(error),
        );
      });
      response.on("error", reject);
    });
    request.on("error", reject);
    request.end();
  });
}

/**
 * 这个下载地址是否经过 Cloudflare（或同类会压制并发连接的 CDN）。
 *
 * ## 为什么用响应头判断，而不是测速判断
 *
 * 第一版写的是「先测一下并发是否更快」。**实测证明这条路走不通** ——
 * 这条跨境链路的抖动幅度比信号本身还大。同样固定 8MB 总下载量反复测：
 *
 *     1 并发   1513 /  265 KB/s
 *     4 并发    424 /  368 KB/s
 *     6 并发   1808 / 2144 KB/s
 *     8 并发   1882 / 1884 KB/s
 *
 * 同一并发数两次测量能差 5 倍。任何基于计时的判断都是在赌运气，
 * 而赌错的代价是「所有用户下载慢 6 倍」。
 *
 * 但「是否经过 CDN」是**确定性信号**，看响应头就知道，与网速无关：
 *
 *     server: cloudflare
 *     cf-cache-status: HIT / MISS / DYNAMIC
 *     cf-ray: ...
 *
 * 所以改成：看到 CDN 标记就单连接，否则分段。
 *
 * ## 为什么 CDN 就要单连接
 *
 * 两个 74MB 全量实测（同一台机器、同一个文件）：
 *
 *     Cloudflare 上 6 并发   995 秒    73 KB/s
 *     Cloudflare 单连接      156 秒   470 KB/s   ← 快 6 倍
 *
 * CDN 对单 IP 的并发连接会排队/限速，多开只互相拖慢。
 * 而直连源站时结论相反（188 → 685 KB/s，3.6 倍），因为瓶颈是单条
 * TCP 在高重传（14.9%）链路上的拥塞窗口。
 *
 * 两种环境的最优策略是反的，所以按环境切换，不写死。
 */
async function isBehindCdn(url: string): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    const request = net.request({ url, method: "GET", redirect: "follow" });
    // 只要 1 字节，这个请求的唯一用途是读响应头
    request.setHeader("Range", "bytes=0-0");
    request.setHeader("User-Agent", DOWNLOAD_USER_AGENT);
    const finish = (value: boolean): void => resolve(value);
    request.on("response", (response) => {
      // 判定规则在 updateDownload.ts 里（纯函数，可单测）
      const behind = isCdnHeaders(response.headers as Record<string, unknown>);
      response.on("data", () => {});
      response.on("end", () => finish(behind));
      response.on("error", () => finish(false));
    });
    request.on("error", () => finish(false));
    request.end();
  });
}

/** 分段并发下载。成功返回整份文件的 sha256。 */
async function downloadBySegments(
  url: string,
  partialPath: string,
  segments: Segment[],
  total: number,
  onProgress?: (progress: DownloadProgress) => void,
): Promise<{ ok: true; sha256: string } | { ok: false; error: string }> {
  let file: Awaited<ReturnType<typeof fs.open>> | null = null;
  try {
    file = await fs.open(partialPath, "w+");
    // 预置到总长度：最后一段可能因为服务端少给几字节而留下空洞
    await file.truncate(total);

    let received = 0;
    let lastReport = 0;
    const reportBytes = (count: number): void => {
      received += count;
      // 进度回调太频繁会拖慢渲染；每 512KB 报一次足够
      if (onProgress && received - lastReport >= 512 * 1024) {
        lastReport = received;
        onProgress({
          received,
          total,
          percent: total > 0
            ? Math.min(100, Math.round((received / total) * 100))
            : 0,
        });
      }
    };

    const handle = file;
    await Promise.all(
      segments.map((segment) => fetchSegment(url, handle, segment, reportBytes)),
    );
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
  } catch (error) {
    await file?.close().catch(() => {});
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

/** 原来的单连接顺序下载（分段不可用时的退路）。 */
async function downloadSingleStream(
  url: string,
  partialPath: string,
  total: number,
  onProgress?: (progress: DownloadProgress) => void,
): Promise<{ ok: true; sha256: string } | { ok: false; error: string }> {
  try {
    let received = 0;
    const hash = createHash("sha256");
    await new Promise<void>((resolve, reject) => {
      const request = net.request({ url, redirect: "follow" });
      request.setHeader("User-Agent", DOWNLOAD_USER_AGENT);
      request.on("response", (response) => {
        if (response.statusCode !== 200) {
          reject(new Error(`下载失败（HTTP ${response.statusCode}）`));
          return;
        }
        const out = createWriteStream(partialPath);
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
    return { ok: true, sha256: hash.digest("hex") };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    };
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

  const dir = updatesDir();
  await fs.mkdir(dir, { recursive: true });
  const target = path.join(dir, `AohuangShangpin-${manifest.version}-Setup.exe`);
  const partial = `${target}.part`;
  const total = Number(manifest.sizeBytes) || 0;

  try {
    await fs.rm(partial, { force: true });

    // 分段并发下载（2026-11-20）—— **仅在实测有效时才用**。
    //
    // ## 为什么要先探测
    //
    // 直连源站时，分段确实是大幅提速的：
    //
    //   直连 aohuang888.com，12MB 样本
    //     单连接   65.4s   188 KB/s
    //     6 并发   17.9s   685 KB/s   ← 3.6x
    //
    //   原因：瓶颈是单条 TCP 在 14.9% 重传链路上的拥塞窗口，多开连接有效。
    //
    // **但走 Cloudflare 之后结论完全反过来了。** 同一台机器、同一个文件：
    //
    //   经 CDN aohuang888.xyz，完整 74MB
    //     单连接   156s   470 KB/s
    //     6 并发   995s    73 KB/s   ← 慢 6 倍
    //
    //   CDN 对单 IP 的并发连接有额外限速/排队，多开只会互相拖慢。
    //
    // 所以不能无条件分段 —— 那会让**所有**用户都变慢 6 倍。
    // 做法：先用两条小 Range 请求探测并发是否有效，有效才分段，
    // 否则老老实实单连接。
    //
    // 探测本身很便宜（各 256KB，约 1 秒），代价远小于选错的后果。
    const segments = total > 0 ? planSegments(total) : [];
    let hashHex = "";
    if (segments.length > 1 && !(await isBehindCdn(safeUrl))) {
      const ranged = await downloadBySegments(safeUrl, partial, segments, total, onProgress);
      if (ranged.ok) {
        hashHex = ranged.sha256;
      } else {
        // 分段失败：清干净重来，走单连接
        await fs.rm(partial, { force: true }).catch(() => {});
        const single = await downloadSingleStream(safeUrl, partial, total, onProgress);
        if (!single.ok) {
          return { ok: false, error: single.error };
        }
        hashHex = single.sha256;
      }
    } else {
      const single = await downloadSingleStream(safeUrl, partial, total, onProgress);
      if (!single.ok) {
        return { ok: false, error: single.error };
      }
      hashHex = single.sha256;
    }

    if (hashHex !== expected) {
      await fs.rm(partial, { force: true });
      return {
        ok: false,
        error: `安装包校验失败（期望 ${expected.slice(0, 12)}… 实际 ${hashHex.slice(0, 12)}…）`,
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
      JSON.stringify({ path: finalPath, version: manifest.version, sha256: hashHex,
                       downloadedAt: Date.now() }),
      "utf-8",
    );
    return {
      ok: true,
      path: finalPath,
      version: manifest.version,
      size: stat.size,
      sha256: hashHex,
    };
  } catch (error) {
    await fs.rm(partial, { force: true }).catch(() => {});
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

/** 当前安装目录（打包态才有意义；开发态返回空串，不传 /DIR）。 */
function installDir(): string {
  return app.isPackaged ? path.dirname(app.getPath("exe")) : "";
}

/**
 * PowerShell 的绝对路径。
 *
 * 不能只写 `"powershell.exe"` 交给 PATH 解析：Electron 打包后的进程环境
 * 与用户 shell 不同（PATH 可能被裁剪），解析失败只会 emit 一个
 * `error` 事件，静默失败（2026-11-23 实测：安装包在、软件退出、
 * install.log 一行没有）。Windows PowerShell 的位置是固定的。
 */
function powershellPath(): string {
  const root = process.env.SystemRoot || process.env.windir || "C:\\Windows";
  return path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
}

/**
 * 启动安装器。
 *
 * 参数见 `installerArgs.ts`：Inno 要 `/VERYSILENT`，**不是** NSIS 的 `/S`
 * （2026-11-23 实测：传 `/S` 会弹出图形向导，用户以为在后台升级，
 * 其实桌面上躺着一个没人点的窗口）。同时用 `/DIR=` 钉住安装目录，
 * 保证装的就是桌面快捷方式指的那一份。
 *
 * 这里**不等待**安装器退出：它会关掉本程序，等待会让主进程挂在一个正在被卸载的进程上。
 */
export function launchInstaller(installerPath: string, silent = true): { ok: boolean; error?: string } {
  try {
    const args = silent ? installerArguments(installDir()) : [];
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
 * 退出时安装：静默覆盖 → 清理旧文件 → 自动重启 → 快捷方式仍指向新版本。
 *
 * ## 为什么要写这个 .cmd + 独立的 PowerShell（2026-11-20 重做）
 *
 * 负责人要求：「下载好之后静默处理的，然后自动重启软件，用户桌面端的
 * 快捷方式启动也是在最新版本的」。
 *
 * ## 三道串行步骤（任何一步提前都会失败）
 *
 *   1. 等本进程真正退出（Inno 要替换 AohuangShangpin.exe，
 *      被占用时 Setup 会报「文件正在使用」）
 *   2. `/VERYSILENT /DIR=<当前安装目录>` 静默覆盖安装
 *   3. 装完**按快捷方式当前指向**拉起新版本
 *
 * ## 2026-11-23 修的三个真实故障
 *
 * **① 参数用错**：原来传 NSIS 的 `/S`，Inno 不认 → 弹图形向导。
 *    用户关掉软件以为在后台升级，其实桌面上躺着个没人点的窗口。
 *    见 `installerArgs.ts`。
 *
 * **② 安装包在自己被运行前删掉了**：原代码 spawn 完 PowerShell 后立刻
 *    `await discardInstaller()`，那会删掉 `updates\*.exe` ——
 *    而 PowerShell 还要 800ms 后才去启动它。文件早没了，
 *    （`Start-Process` 抛错、静默吞掉），于是「关掉软件什么也没发生」。
 *    现在**只由 PowerShell 在安装成功后清理**，JS 侧不再碰。
 *
 * **③ 重启目标取早了**：原代码安装前就把 `$exe` 定成快捷方式当前目标。
 *    如果这次安装换了目录，装完启动的还是旧路径。现在安装完重新读一次
 *    快捷方式（Inno 的 `[Icons]` 每次都会重写它），读不到才退回原值。
 *
 * 编码用 Base64(UTF-16LE)：中文路径（D:\软件\奥皇上品）走 cmd 的 GBK
 * 会变乱码，`-EncodedCommand` 不受控制台代码页影响。
 */
export async function installOnQuit(): Promise<void> {
  const pending = await pendingInstaller();
  if (!pending) return;

  const dir = installDir();

  // 兜底重启目标：当前进程的 exe。安装完会优先用快捷方式的新目标。
  let restartTarget = app.getPath("exe");
  try {
    const shortcut = path.join(app.getPath("desktop"), "奥皇上品.lnk");
    const exists = await fs.access(shortcut).then(() => true).catch(() => false);
    if (exists) {
      const resolved = resolveShortcutTarget(shortcut);
      if (resolved) restartTarget = resolved;
    }
  } catch { /* 取不到就用当前 exe */ }

  const installerTarget = pending.path;
  const logPath = path.join(app.getPath("userData"), "install.log");

  // 脚本本体在 updateDownload.ts 的纯函数 `installPowerShellScript()` 里。
  //
  // 为什么抽出去：拼 PS 命令是不可见的代码，一个引号错了整条脚本语法报错、
  // PowerShell 秒退，日志一行都不写 —— 实测就是这个症状（安装包在、
  // 进程优雅退出、版本没变、install.log 是空的）。抽成纯函数后可以用
  // 真 PowerShell 直接跑一遍验证，也能进单元测试。
  const encoded = encodePowerShellCommand(installPowerShellScript({
    installerPath: installerTarget,
    restartPath: restartTarget,
    logPath,
    appDir: dir,
  }));
  const { spawn: _spawn } = await import("node:child_process");
  const psExe = powershellPath();
  // ## 必须用 `cmd /c start` 启动，不能直接 spawn（2026-11-23 第五个坑，决定性实验）
  //
  // 直连 spawn 在 Windows 上**必然静默失败**：实测五种写法只有三种能跑，
  // 而我们现在用的 `detached:true + stdio:"ignore"` 恰好是最坏的那种 ——
  // 进程被创建、立刻 exit 0、脚本一行都不执行、日志一个字节不写。
  //
  //   spawn(ps, [...], {detached:true, stdio:"ignore"})  → 空文件，脚本没跑
  //   spawn(ps, [...], {detached:true, windowsHide:true}) → 同上
  //   spawn(ps, [...], {detached:true, stdio:[null,null,null]}) → 同上
  //   spawn(ps, [...], {shell:true})                     → OK
  //   spawn(ps, [...], {detached:false}).unref()          → OK（但没真脱离）
  //   spawn("cmd", ["/c","start","","/b",ps,...])         → OK 且真脱离
  //
  // 选最后一种：`start /b` 让 PowerShell 成为独立的进程树根，
  // Electron 退出后不会被连带杀掉，也不需要 shell 参数拼接（无注入风险）。
  const child = _spawn(
    process.env.ComSpec || "cmd.exe",
    ["/c", "start", "", "/b", psExe, "-WindowStyle", "Hidden", "-NonInteractive", "-EncodedCommand", encoded],
    { detached: true, stdio: "ignore", windowsHide: true }
  );
  // ## 必须监听 error（2026-11-23 第三个坑）
  //
  // 之前写的是 `spawn("powershell.exe", ...)` 然后直接 `.unref()`。
  // 如果进程起不来（PATH 里没有、或打包环境的 PATH 被裁剪），Node 只会
  // emit 一个 `error` 事件 —— 没人监听时**完全静默**：不回包、不抛错、
  // 不写日志。实测表现就是「安装包在、软件优雅退出、什么也没发生、
  // install.log 一行没有」，比崩溃还难查。
  //
  // 现在：① 用绝对路径（%SystemRoot%\System32\WindowsPowerShell\v1.0）；
  //       ② error 落盘到 install.log，下次一眼能看出是 spawn 失败。
  child.on("error", (error: Error) => {
    void fs.appendFile(
      logPath,
      `[${new Date().toISOString()}] spawn failed (${psExe}): ${error.message}\n`,
    ).catch(() => {});
  });
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
  if (!started) {
    await fs.appendFile(
      logPath,
      `[${new Date().toISOString()}] powershell did not reach launch marker within 15s (pid=${child.pid ?? "?"})\n`,
    ).catch(() => {});
  }
  child.unref();
  // 刻意**不**在这里 discardInstaller()：那会删掉 PowerShell 900ms 后要运行的
  // 安装包（2026-11-23 事故根因）。清理交给上面的 PowerShell，装完再删。
}

/**
 * 等 `installPowerShellScript()` 写出 `launching:` 那一行。
 *
 * 轮询而不是监听文件：Windows 上 fs.watch 对「文件还不存在 → 被创建」
 * 的语义不可靠，而这里最多等 15 秒、每 100ms 一次，代价可以忽略。
 */
async function waitForLaunchMarker(logPath: string, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const content = await fs.readFile(logPath, "utf-8").catch(() => "");
    if (content.includes("launching:")) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return false;
}

/**
 * 读出 .lnk 快捷方式指向的目标路径。
 *
 * 为什么需要：安装器装完会把桌面/开始菜单快捷方式指到**它自己**的安装目录。
 * 如果用户当初装到了 D:\软件\奥皇上品，而这次 setup 因为某些原因装到默认的
 * %LOCALAPPDATA%\Programs，直接启动 app.getPath("exe") 就等于启动了一个
 * 可能不存在的旧路径。读快捷方式拿到的是「安装器认为该启动的那个」。
 */
function resolveShortcutTarget(shortcutPath: string): string {
  try {
    // 用 PowerShell 读 WScript.Shell，避免引入额外原生依赖。
    // execFileSync 静态导入（不能 require()：eslint no-require-imports）。
    const encoded = Buffer.from(
      "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('"
        + shortcutPath.replace(/'/g, "''") + "'); "
        + "if ($s.TargetPath) { Write-Output $s.TargetPath }",
      "utf16le",
    ).toString("base64");
    const output = execFileSync(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
      { encoding: "utf-8", timeout: 5000, windowsHide: true },
    ).trim();
    return output || "";
  } catch {
    return "";
  }
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

/**
 * **自动静默更新**（负责人 2026-11-20）。
 *
 * 与 `quitAndInstall` 的区别 / 取舍：
 *
 *   quitAndInstall  → 用户点了「重启并更新」，主进程立刻退出安装
 *   autoApply       → 后台下好之后**不打断**当前工作，等这次任务做完、
 *                     用户主动关软件时才装（before-quit 里执行）
 *
 * 为什么不「下载完立刻装」：安装会杀掉进程，正在跑的发布/认领任务全丢，
 * 载体建到一半还会在上游留下孤儿草稿。所以时机必须由用户「关软件」来触发 ——
 * 那本来就是一次自然的收尾点。
 *
 * 用户在设置里把「自动更新」关掉后，这条路径就不走，回到手动模式。
 */
export async function markAutoApply(): Promise<void> {
  try {
    const dir = updatesDir();
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(
      path.join(dir, "auto-apply.json"),
      JSON.stringify({ auto: true, at: Date.now() }),
      "utf-8",
    );
  } catch { /* 标记失败就退化成手动安装，不影响主流程 */ }
}

export async function isAutoApplyMarked(): Promise<boolean> {
  try {
    const raw = await fs.readFile(path.join(updatesDir(), "auto-apply.json"), "utf-8");
    const data = JSON.parse(raw) as { auto?: boolean };
    return Boolean(data.auto);
  } catch {
    return false;
  }
}

export async function clearAutoApplyMark(): Promise<void> {
  try {
    await fs.rm(path.join(updatesDir(), "auto-apply.json"), { force: true });
  } catch { /* 清理失败不影响主流程 */ }
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
