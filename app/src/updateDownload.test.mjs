/**
 * 分段下载的核心逻辑测试。
 *
 * 2026-11-20 负责人反馈「下载更新很慢」，实测本机到香港服务器：
 *
 *   单连接   65.4s   188 KB/s
 *   6 并发   17.9s   685 KB/s   ← 3.6x
 *
 * 所以改成 HTTP Range 分段并发。这里钉住三件最容易出错的事：
 *
 * 1. **切片必须无缝覆盖整个文件** —— 少一个字节就会留下零字节空洞，
 *    sha256 必然不符，但报错会指向「校验失败」，让人误以为服务器给错包。
 * 2. **最后一段必须正好到 total-1** —— 超出会 416，不足会缺数据。
 * 3. **白名单必须精确匹配** —— 用 endsWith/includes 会让
 *    `aohuang888.xyz.evil.com` 通过，等于白名单形同虚设。
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  ALLOWED_HOSTS,
  DOWNLOAD_CONCURRENCY,
  MIN_SEGMENT_BYTES,
  planSegments,
  rangeHeader,
  segmentLength,
  segmentsCoverExactly,
  validateUpdateUrl,
} from "../src/updateDownload.ts";


// --- 下载地址白名单 ---------------------------------------------------------

test("accepts both official hosts over https", () => {
  for (const host of ALLOWED_HOSTS) {
    const url = `https://${host}/miaoshou-downloads/AohuangShangpin-0.1.30-Setup.exe`;
    assert.equal(validateUpdateUrl(url), url, host);
  }
});

test("rejects lookalike hosts that merely contain the official name", () => {
  // 这些都是「包含」但不等价的域名。用 endsWith / includes 就会放行。
  const evil = [
    "https://evilaohuang888.xyz/x.exe",
    "https://aohuang888.xyz.evil.com/x.exe",
    "https://notaohuang888.com/x.exe",
    "https://aohuang888.com.evil.cn/x.exe",
    "https://xaohuang888.xyz/x.exe",
  ];
  for (const url of evil) {
    assert.equal(validateUpdateUrl(url), null, url);
  }
});

test("rejects http, non-443 ports, credentials and non-exe paths", () => {
  assert.equal(validateUpdateUrl("http://aohuang888.xyz/x.exe"), null);
  assert.equal(validateUpdateUrl("https://aohuang888.xyz:8443/x.exe"), null);
  assert.equal(validateUpdateUrl("https://user:pw@aohuang888.xyz/x.exe"), null);
  assert.equal(validateUpdateUrl("https://aohuang888.xyz/x.zip"), null);
  assert.equal(validateUpdateUrl("https://aohuang888.xyz/x.exe.sha256"), null);
  assert.equal(validateUpdateUrl("not a url"), null);
  assert.equal(validateUpdateUrl(""), null);
});

test("explicit port 443 is allowed", () => {
  // `new URL()` 会把默认端口 443 去掉，所以返回的是不带端口的形式。
  // 关键：显式写 :443 能通过校验（不算「非常规端口」）。
  assert.equal(validateUpdateUrl("https://aohuang888.xyz:443/x.exe"),
    "https://aohuang888.xyz/x.exe");
});

test("hostname comparison is case-insensitive", () => {
  // `new URL()` 会把 hostname 规范化为小写，所以返回值是小写形式。
  // 关键是「大写输入也能通过校验」，而不是返回原样字符串。
  assert.equal(validateUpdateUrl("https://AOHUANG888.XYZ/x.exe"),
    "https://aohuang888.xyz/x.exe");
  assert.equal(validateUpdateUrl("https://Aohuang888.Com/x.exe"),
    "https://aohuang888.com/x.exe");
});


// --- 切片规划 ---------------------------------------------------------------

test("small files are not segmented", () => {
  // 小于 2 × MIN_SEGMENT_BYTES 不分段：请求开销大于收益
  assert.deepEqual(planSegments(0), []);
  assert.deepEqual(planSegments(-1), []);
  assert.deepEqual(planSegments(MIN_SEGMENT_BYTES), []);
  assert.deepEqual(planSegments(MIN_SEGMENT_BYTES * 2 - 1), []);
});

test("real installer size covers every byte exactly once", () => {
  // 0.1.29 安装包的真实大小
  const total = 74819729;
  const segments = planSegments(total);
  assert.ok(segments.length > 1, segments.length);
  assert.ok(segmentsCoverExactly(segments, total),
    `切片不连续或未覆盖：${JSON.stringify(segments)}`);
  // 不允许重叠：累加长度必须正好等于总长
  const sum = segments.reduce((acc, item) => acc + segmentLength(item), 0);
  assert.equal(sum, total);
});

test("segments are contiguous across many sizes", () => {
  for (const total of [
    MIN_SEGMENT_BYTES * 2,
    MIN_SEGMENT_BYTES * 3 + 1,
    MIN_SEGMENT_BYTES * 5,
    100_000_000,
    18_000_000,
    191_000_000,
  ]) {
    const segments = planSegments(total);
    if (segments.length === 0) continue;
    assert.ok(segmentsCoverExactly(segments, total), `total=${total}`);
  }
});

test("never exceeds configured concurrency", () => {
  const segments = planSegments(700 * 1024 * 1024);
  assert.ok(segments.length <= DOWNLOAD_CONCURRENCY, segments.length);
});

test("last segment ends exactly at total-1", () => {
  const total = 74_819_729;
  const segments = planSegments(total);
  assert.equal(segments[segments.length - 1].end, total - 1);
});

test("first segment starts at 0", () => {
  const segments = planSegments(74_819_729);
  assert.equal(segments[0].start, 0);
});

test("range header format is what servers expect", () => {
  assert.equal(rangeHeader({ start: 0, end: 1023 }), "bytes=0-1023");
  assert.equal(rangeHeader({ start: 5000, end: 9999 }), "bytes=5000-9999");
});

test("segmentLength is inclusive on both ends", () => {
  assert.equal(segmentLength({ start: 0, end: 0 }), 1);
  assert.equal(segmentLength({ start: 0, end: 1023 }), 1024);
  assert.equal(segmentLength({ start: 100, end: 199 }), 100);
});

test("segmentsCoverExactly rejects gaps and overlaps", () => {
  const total = 100;
  // 正常
  assert.equal(segmentsCoverExactly([{ start: 0, end: 49 }, { start: 50, end: 99 }], total), true);
  // 中间缺一个字节
  assert.equal(segmentsCoverExactly([{ start: 0, end: 48 }, { start: 50, end: 99 }], total), false);
  // 重叠
  assert.equal(segmentsCoverExactly([{ start: 0, end: 50 }, { start: 50, end: 99 }], total), false);
  // 没到结尾
  assert.equal(segmentsCoverExactly([{ start: 0, end: 98 }], total), false);
  // 不从 0 开始
  assert.equal(segmentsCoverExactly([{ start: 1, end: 99 }], total), false);
  // 空
  assert.equal(segmentsCoverExactly([], total), false);
});

test("handles non-integer totals defensively", () => {
  assert.deepEqual(planSegments(Number.NaN), []);
  assert.deepEqual(planSegments(Number.POSITIVE_INFINITY), []);
  assert.deepEqual(planSegments(0.5), []);
});


// --- 白名单内容 -------------------------------------------------------------

test("new download domain is whitelisted while main domain stays", () => {
  // 主域必须留着：老版本客户端只认 aohuang888.com
  assert.ok(ALLOWED_HOSTS.includes("aohuang888.com"));
  // 2026-11-20 为下载 CDN 新买的域名
  assert.ok(ALLOWED_HOSTS.includes("aohuang888.xyz"));
  // 不能因为买新域名就把别的顺手加进来
  assert.equal(ALLOWED_HOSTS.length, 2);
});