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