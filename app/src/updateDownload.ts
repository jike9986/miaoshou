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