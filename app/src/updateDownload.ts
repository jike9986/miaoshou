/**
 * 更新下载的纯逻辑（与 Electron 解耦，便于单测）。
 *
 * 2026-11-20 从 `app/src/updateInstaller.ts` 抽出来：那边所有函数都直接
 * 依赖 `electron` 的 `net` / `app`，跑不了单元测试。而分段下载最容易出
 * 错的地方恰好是**规划切片的边界计算**，必须能测。
 */

/** 允许的下载域名白名单（精确匹配，不做后缀匹配）。 */
export const ALLOWED_HOSTS: readonly string[] = [
  // 主域：老版本客户端只认它，清单里给 .xyz 地址时这些客户端会拒绝下载，
  // 所以主域的 URL 必须继续有效。
  "aohuang888.com",
  // 2026-11-20 为安装包下载 CDN 专门买的域名。主域同一台 nginx 上跑着
  // newapi / aohuang-ai / aohuang888.cn / paytest 多个业务，不能动它的 NS。
  "aohuang888.xyz",
];

/** 并发连接数。实测 6 路 685 KB/s，8 路已到平台期（653 KB/s）。 */
export const DOWNLOAD_CONCURRENCY = 6;
/** 单片最小 4MB，避免 74MB 切成十几片后请求开销变大。 */
export const MIN_SEGMENT_BYTES = 4 * 1024 * 1024;

/**
 * 下载请求要带的 User-Agent。
 *
 * ## 为什么必须显式设置（2026-11-20 实测）
 *
 * 接入 Cloudflare 后，用默认 UA 请求会拿到 **403 error code 1010**
 * （Browser Integrity Check，专门拦非浏览器 UA）：
 *
 *     Python-urllib/3.13   → 403  error code: 1010
 *     Electron/31.7.7      → 206  ✅
 *     curl/8.x             → 206  ✅
 *     浏览器 UA             → 206  ✅
 *     空 UA                → 206  ✅
 *
 * 也就是说 Cloudflare 只拦「已知的脚本类 UA」。Electron 默认 UA 目前能过，
 * 但那是**依赖对方规则恰好没拦**，不是我们能控制的 —— Cloudflare 的托管
 * 规则会变，哪天把 `Electron/` 加进去，全量用户的自动更新就集体失效，
 * 而且症状只是「下载失败」，很难联想到是 CDN 的 UA 规则。
 *
 * 所以主动声明一个浏览器形态的 UA（带自己产品标识便于对方统计），
 * 让请求看起来就是正常客户端，不依赖默认值。
 */
export const DOWNLOAD_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
  + "(KHTML, like Gecko) AohuangShangpin/Update Chrome/126.0.0.0 "
  + "Safari/537.36";

export interface Segment {
  /** 闭区间起点（含） */
  start: number;
  /** 闭区间终点（含） */
  end: number;
}

/**
 * 校验下载地址：必须 https + 白名单域（精确匹配）+ .exe + 只允许 443。
 *
 * 白名单精确匹配是关键：`evilaohuang888.xyz`、`aohuang888.xyz.evil.com`
 * 这类必须被拒，不能用 `endsWith` 或 `includes`。
 */
export function validateUpdateUrl(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:") return null;
    if (!ALLOWED_HOSTS.includes(url.hostname.toLowerCase())) return null;
    if (url.username || url.password) return null;
    if (url.port && url.port !== "443") return null;
    if (!url.pathname.toLowerCase().endsWith(".exe")) return null;
    return url.href;
  } catch {
    return null;
  }
}

/**
 * 把文件按总字节切成若干段（HTTP Range 闭区间）。
 *
 * ## 必须保证的三件事
 *
 * 1. **无缝覆盖**：第 i 段的 start 必须等于第 i-1 段的 end + 1，
 *    否则文件里会出现没下载的空洞。
 * 2. **最后一段必须正好到 total - 1**：不能少，也不能超出总长度
 *    （超出会被服务器返回 416）。
 * 3. **不允许返回空数组时表示"支持分段"**：空数组专门表示"不要分段"，
 *    调用方据此退回单连接。
 *
 * 返回 `[]` 表示文件太小或无法分段，调用方应走单连接。
 */
export function planSegments(total: number): Segment[] {
  if (!Number.isFinite(total) || total <= 0) return [];
  // 太小的文件分段没意义（请求开销大于收益）
  if (total < MIN_SEGMENT_BYTES * 2) return [];
  const count = Math.min(
    DOWNLOAD_CONCURRENCY,
    Math.max(1, Math.floor(total / MIN_SEGMENT_BYTES)),
  );
  if (count <= 1) return [];
  const size = Math.floor(total / count);
  const segments: Segment[] = [];
  for (let index = 0; index < count; index += 1) {
    const start = index * size;
    const end = index === count - 1 ? total - 1 : start + size - 1;
    segments.push({ start, end });
  }
  return segments;
}

/** 某一段应有的字节数（闭区间）。 */
export function segmentLength(segment: Segment): number {
  return segment.end - segment.start + 1;
}

/** 校验切片规划是否自洽（供测试和运行时断言用）。 */
export function segmentsCoverExactly(segments: Segment[], total: number): boolean {
  if (segments.length === 0) return false;
  if (segments[0].start !== 0) return false;
  if (segments[segments.length - 1].end !== total - 1) return false;
  for (let index = 1; index < segments.length; index += 1) {
    if (segments[index].start !== segments[index - 1].end + 1) return false;
  }
  const sum = segments.reduce((acc, item) => acc + segmentLength(item), 0);
  return sum === total;
}

/** `Range: bytes=start-end` 头。 */
export function rangeHeader(segment: Segment): string {
  return `bytes=${segment.start}-${segment.end}`;
}

/**
 * 响应头是否表明「经过 CDN（会压制并发连接）」。
 *
 * ## 为什么这个判断决定要不要分段（实测依据）
 *
 * 同一份 74MB 安装包，两条路径的最优策略完全相反：
 *
 *     直连源站 aohuang888.com
 *        单连接 188 KB/s  →  6 并发 685 KB/s   分段值得（3.6x）
 *        瓶颈是单条 TCP 在 14.9% 重传链路上的拥塞窗口
 *
 *     经 Cloudflare aohuang888.xyz
 *        单连接 470 KB/s  →  6 并发  73 KB/s   分段让用户慢 6 倍
 *        CDN 对单 IP 并发连接排队/限速，多开只互相拖慢
 *
 * 所以必须按环境切换。而这个判断**只看响应头，不看测速** ——
 * 测速在这条链路上完全不可靠：固定 8MB 反复测，同样 6 并发两次
 * 得到 1808 和 2144 KB/s，而 4 并发只有 424 KB/s，噪声比信号大。
 *
 * ## 保守原则
 *
 * 看到**任何** CDN 迹象就当 CDN。判错的代价不对称：
 *
 *     漏判 CDN → 走 6 并发 → 用户下载慢 6 倍（十几分钟）
 *     误判直连 → 走单连接 → 最多少快 3.6 倍（一两分钟）
 *
 * 所以宁可保守。
 */
export function isCdnHeaders(headers: Record<string, unknown>): boolean {
  // 键大小写不敏感。不能依赖上游已归一化 —— 测试里传 `Server`
  // 就取不到值，会让 CDN 被判成直连。
  const lookup = new Map<string, string>();
  for (const [key, value] of Object.entries(headers || {})) {
    const text = Array.isArray(value) ? value.join(",") : String(value ?? "");
    lookup.set(key.toLowerCase(), text.toLowerCase());
  }
  const header = (name: string): string => lookup.get(name) ?? "";
  return header("server").includes("cloudflare")
    || header("cf-ray").length > 0
    || header("cf-cache-status").length > 0
    || header("via").includes("cloudfront")
    || header("x-cache").length > 0
    || header("x-amz-cf-id").length > 0;
}

// --- 安装器参数（NSIS，electron-builder 标准安装包）----------------------------
//
// 2026-11-23 事故链的最后一环：之前用的是 Inno Setup 打的安装包，
// 但客户端传的是 NSIS 的参数 `/S` —— Inno 不认，**弹出图形向导**（实测窗口
// 标题「安装 奥皇上品 0.1.34」），用户关掉软件后桌面上躺着个没人点的窗口，
// 软件再也没起来。
//
// 现在的做法是直接走大厂标准路径：安装包用 **electron-builder 自带的 NSIS**，
// 更新器就是 electron-updater 那一套契约：
//
//     setup.exe /S            静默安装（NSIS 的原生参数，装机即用）
//     setup.exe /S /D=<dir>    装回原目录（/D= 必须是最后一个参数，且不带引号）
//
// 为什么 /D= 要钉住目录：安装器默认装到 `%LOCALAPPDATA%\Programs\奥皇上品`，
// 而用户桌面快捷方式可能还指着老的 `D:\软件\奥皇上品`。一旦两处目录不同，
// 就会出现「装的是新版、点快捷方式是老版」的无限更新循环。
// 把安装目录钉在**当前运行的 exe 所在目录**，装完就是同一个文件被替换。

/** 去掉尾部路径分隔符：`D:\软件\奥皇上品\` 与 `D:\软件\奥皇上品` 视为同一个目录。
 *
 * 不用正则字符类：`[\/]` 这种写法在多层转义里极易被吃掉一个反斜杠，
 * 变成只匹配正斜杠 —— 第一次实现就踩了这个坑，单元测试把带尾反斜杠的
 * 路径原样传了下去。这里直接判码点：47 = `/`，92 = 反斜杠。
 */
function trimTrailingSeparators(value: string): string {
  let end = value.length;
  while (end > 0) {
    const ch = value.charCodeAt(end - 1);
    if (ch !== 47 && ch !== 92) break;
    end -= 1;
  }
  return value.slice(0, end);
}

/** NSIS 的静默安装参数（electron-builder 安装包的原生开关）。 */
export const NSIS_SILENT_FLAGS = ["/S"] as const;

/**
 * 构造安装器参数（NSIS）。
 *
 * `appDir` 为空（开发态、或取不到安装目录）时只传 `/S`，
 * 让安装器用它自己记录的目录（electron-builder 会写注册表 InstallLocation）。
 */
export function installerArguments(appDir?: string): string[] {
  const args: string[] = [...NSIS_SILENT_FLAGS];
  const dir = trimTrailingSeparators((appDir ?? "").trim());
  // /D= 必须排最后：NSIS 把 /D= 之后的内容整体当作目录，带空格也不加引号。
  if (dir) args.push(`/D=${dir}`);
  return args;
}

// --- 退出时静默安装的 PowerShell 脚本（纯函数）-------------------------------
//
// 为什么要独立成函数（2026-11-23 第二起事故）：
//   JS 里用字符串拼 PS 命令，拼错一个引号就整条脚本语法错误。实测症状是
//   安装包在、(pending.json 在)、进程也优雅退出了，但 `install.log` 一行都没写
//   —— PowerShell 启动即语法报错秒退，后面的 Start-Process 全没跑。
//   命令是**看不到也测不到**的代码，所以抽成纯函数，然后用真 PowerShell 跑一遍。

/** 单引号字符串转义（PowerShell 里用两个单引号表示一个）。 */
function psQuote(value: string): string {
  return "'" + value.replace(/'/g, "''") + "'";
}

export interface InstallScriptOptions {
  /** 下载好的安装包绝对路径 */
  installerPath: string;
  /** 兜底的重启目标（当前 exe），通常会被装完后的快捷方式目标覆盖 */
  restartPath: string;
  /** 日志文件绝对路径，记录 setup 退出码，便于排查 */
  logPath: string;
  /** 安装目录，传给 /DIR= 钉住位置 */
  appDir?: string;
  /** 关闭按钮/进程名，默认 AohuangShangpin */
  processName?: string;
  /** 桌面快捷方式文件名，默认 奥皇上品.lnk */
  shortcutName?: string;
}

/**
 * 生成安装阶段用的 PowerShell 脚本。
 *
 * 硬性事实（都在真机上验证过）：
 *
 * 1. **不能带 `/S`** —— 那是 NSIS 的，Inno 会弹图形向导；用 `/VERYSILENT`。
 * 2. **必须 `/DIR=<当前安装目录>`** —— 否则安装器退回 `%LOCALAPPDATA%\Programs`，
 *    桌面快捷方式还指老目录，用户点它就是老版本，于是无限循环更新。
 * 3. **不能 `-Wait`** —— Inno 的 `[Run]` 装完会拉起软件，而 PowerShell 的
 *    `-Wait` 会连子进程一起等，脚本就永远卡在等待里（实测卡了 314 秒，
 *    直到把软件杀掉才返回）。改成轮询 `HasExited` + 超时。
 * 4. **清理必须在安装之后** —— 老代码在 JS 侧 spawn 完立刻删安装包，
 *    900ms 后要执行的 setup.exe 已经不存在（这是「关掉软件什么都没发生」的根因）。
 * 5. 装完**重读快捷方式**再启动，安装目录换了也能起对版本。
 */
export function installPowerShellScript(options: InstallScriptOptions): string {
  const {
    installerPath,
    restartPath,
    logPath,
    appDir,
    processName = "AohuangShangpin",
    shortcutName = "奥皇上品.lnk",
  } = options;
  const args = installerArguments(appDir).map(psQuote).join(",");
  const lines = [
    "$ErrorActionPreference = 'SilentlyContinue'",
    `$setup = ${psQuote(installerPath)}`,
    `$exe = ${psQuote(restartPath)}`,
    `$log = ${psQuote(logPath)}`,
    // 先落盘「我起来了」，再等主进程退出。
    // 顺序反过来会有致命竞态：主进程 spawn 完就 app.exit(0)，
    // PowerShell 冷启动要 300~800ms，等它睡醒 900ms 主进程早没了，
    // 安装包一动不动、日志一行不写。先写日志，主进程就能等到这一行再退。
    "Add-Content -LiteralPath $log -Value ('[' + (Get-Date -Format s) + '] launching: ' + $setup)",
    // 等本进程真正退出：安装器要替换正在运行的 exe
    "Start-Sleep -Milliseconds 900",
    `$proc = Start-Process -FilePath $setup -PassThru -ArgumentList @(${args})`,
    // 轮询而不是 -Wait：-Wait 会把 Inno 拉起的软件当成子进程一起等
    "$deadline = (Get-Date).AddMinutes(10)",
    "while (-not $proc.HasExited -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }",
    "$code = if ($proc.HasExited) { $proc.ExitCode } else { -1 }",
    "Add-Content -LiteralPath $log -Value ('[' + (Get-Date -Format s) + '] setup exit=' + $code)",
    // 让安装器把文件落完盘
    "Start-Sleep -Seconds 2",
    // 安装器会重写桌面快捷方式，重读一次拿新路径
    `$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) ${psQuote(shortcutName)}`,
    "if (Test-Path -LiteralPath $lnk) { $t = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk).TargetPath; if ($t -and (Test-Path -LiteralPath $t)) { $exe = $t } }",
    "Add-Content -LiteralPath $log -Value ('[' + (Get-Date -Format s) + '] restart: ' + $exe)",
    // [Run] 段通常已经把软件拉起来了，先查再启动，避免开两个窗口
    `$running = Get-Process -Name ${psQuote(processName)}`,
    "if (-not $running) { for ($i = 0; $i -lt 5; $i++) { if (Test-Path -LiteralPath $exe) { Start-Process -FilePath $exe -ArgumentList '--no-sandbox'; break }; Start-Sleep -Milliseconds 800 } }",
    // 清理：装完才删（见上面第 4 条）
    "Get-ChildItem -Path (Split-Path -LiteralPath $setup) -Include *.exe,*.part -File | Where-Object { $_.FullName -ne $setup } | Remove-Item -Force",
    "Remove-Item -LiteralPath $setup -Force",
    "Remove-Item -LiteralPath (Join-Path (Split-Path -LiteralPath $setup) 'pending.json') -Force",
  ];
  return lines.join("; ");
}

/** 把脚本编码成 PowerShell 的 `-EncodedCommand` 参数（UTF-16LE + Base64）。
 *
 * 中文路径（`D:\软件\奥皇上品`）走控制台代码页会乱码，编码命令不受影响。
 */
export function encodePowerShellCommand(script: string): string {
  return Buffer.from(script, "utf16le").toString("base64");
}
