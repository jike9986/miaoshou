// design-system §3.3 数字排版 · §3.4 文本规则 · §0 落地基线
// 通用工具：类名合并 + 数字/价格/时间格式化。
// 规则来源：金额为字符串十进制（api.md §0.1），时间为 Unix 秒（浮点）。

import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * 合并 className，后者的 tailwind 语义类覆盖前者同族类。
 * 用法：cn("px-2", isActive && "px-4") → "px-4"
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/** 空值统一占位符（design-system §3.4：不用「无 / null / -」） */
export const EMPTY_PLACEHOLDER = "—";

export type DateInput = number | string | Date | null | undefined;
export type NumericInput = number | string | null | undefined;

/* ------------------------------------------------------------------ *
 * 价格 / 数字
 * ------------------------------------------------------------------ */

/** 货币符号（design-system §3.3：¥ 与数字之间无空格） */
export const CURRENCY_SYMBOLS: Record<string, string> = {
  CNY: "¥",
  MYR: "RM",
  SGD: "S$",
  USD: "$",
  EUR: "€",
  THB: "฿",
  VND: "₫",
  IDR: "Rp",
  PHP: "₱",
};

export function currencySymbol(currency?: string | null): string {
  if (!currency) return CURRENCY_SYMBOLS.CNY;
  return CURRENCY_SYMBOLS[currency.toUpperCase()] ?? "";
}

function toNumber(value: NumericInput): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

export interface FormatPriceOptions {
  /** 货币代码，默认 CNY */
  currency?: string | null;
  /** 直接指定符号，优先于 currency */
  symbol?: string | null;
  /** 小数位，默认 2（金额保留 2 位，库存走 formatCount） */
  decimals?: number;
  /** 是否带货币符号，默认 true */
  withSymbol?: boolean;
}

/**
 * 格式化价格。入参可为后端返回的字符串十进制（"35.90"）。
 * formatPrice("1299")            → "¥1,299.00"
 * formatPrice("35.9", {currency:"MYR"}) → "RM35.90"
 * formatPrice(null)              → "—"
 */
export function formatPrice(value: NumericInput, options: FormatPriceOptions = {}): string {
  const n = toNumber(value);
  if (n === null) return EMPTY_PLACEHOLDER;
  const { currency = "CNY", symbol = null, decimals = 2, withSymbol = true } = options;
  const amount = formatDecimal(n, decimals);
  if (!withSymbol) return amount;
  return `${symbol ?? currencySymbol(currency)}${amount}`;
}

/** 千分位 + 固定小数位 */
export function formatDecimal(value: number, decimals = 2): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export interface FormatCountOptions {
  /** 是否缩写为「万 / 亿」，默认 false（§3.3 要求 ≥10000 用千分位） */
  compact?: boolean;
  /** 缩写保留位数，默认 1 */
  digits?: number;
}

/**
 * 整数计数（库存 / 销量 / 条数）。
 * formatCount(1284)            → "1,284"
 * formatCount(12840, {compact:true}) → "1.3万"
 */
export function formatCount(value: NumericInput, options: FormatCountOptions = {}): string {
  const n = toNumber(value);
  if (n === null) return EMPTY_PLACEHOLDER;
  const { compact = false, digits = 1 } = options;
  if (compact) {
    const abs = Math.abs(n);
    if (abs >= 1e8) return `${trimZero(n / 1e8, digits)}亿`;
    if (abs >= 1e4) return `${trimZero(n / 1e4, digits)}万`;
  }
  return Math.round(n).toLocaleString("en-US");
}

function trimZero(value: number, digits: number): string {
  return value.toFixed(digits).replace(/\.0+$/, "");
}

/* ------------------------------------------------------------------ *
 * 时间（Unix 秒 → 展示）
 * ------------------------------------------------------------------ */

/** Unix 秒 / 毫秒 / ISO 字符串 / Date → Date，无法解析返回 null */
export function toDate(input: DateInput): Date | null {
  if (input === null || input === undefined || input === "") return null;
  if (input instanceof Date) return Number.isNaN(input.getTime()) ? null : input;
  if (typeof input === "number") {
    if (!Number.isFinite(input)) return null;
    // api.md：时间戳统一为 Unix 秒（浮点），超过 1e11 视为毫秒
    const ms = Math.abs(input) < 1e11 ? input * 1000 : input;
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const d = new Date(input);
  return Number.isNaN(d.getTime()) ? null : d;
}

const pad = (n: number): string => String(n).padStart(2, "0");

/**
 * 格式化时间，pattern 支持 YYYY MM DD HH mm ss。
 * 默认 "YYYY-MM-DD HH:mm"；表格「更新时间」列用 "MM-DD HH:mm"。
 */
export function formatDate(input: DateInput, pattern = "YYYY-MM-DD HH:mm"): string {
  const d = toDate(input);
  if (!d) return EMPTY_PLACEHOLDER;
  const map: Record<string, string> = {
    YYYY: String(d.getFullYear()),
    MM: pad(d.getMonth() + 1),
    DD: pad(d.getDate()),
    HH: pad(d.getHours()),
    mm: pad(d.getMinutes()),
    ss: pad(d.getSeconds()),
  };
  return pattern.replace(/YYYY|MM|DD|HH|mm|ss/g, (token) => map[token] ?? token);
}

/** 仅时刻 HH:mm */
export function formatTime(input: DateInput): string {
  return formatDate(input, "HH:mm");
}

/** 相对时间：刚刚 / 12分钟前 / 3小时前 / 昨天 14:30 / 3天前 / 2026-10-27 */
export function formatRelative(input: DateInput, now: DateInput = Date.now()): string {
  const d = toDate(input);
  const base = toDate(now) ?? new Date();
  if (!d) return EMPTY_PLACEHOLDER;

  const diffMs = base.getTime() - d.getTime();
  const future = diffMs < 0;
  const absSec = Math.floor(Math.abs(diffMs) / 1000);

  if (absSec < 60) return future ? "即将" : "刚刚";
  if (absSec < 3600) {
    const m = Math.floor(absSec / 60);
    return future ? `${m}分钟后` : `${m}分钟前`;
  }
  if (absSec < 86400) {
    const h = Math.floor(absSec / 3600);
    return future ? `${h}小时后` : `${h}小时前`;
  }

  // 按自然日判断「昨天 / 前天」，避免 23 小时前被算成昨天
  const startOfDay = (v: Date): number =>
    new Date(v.getFullYear(), v.getMonth(), v.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(base) - startOfDay(d)) / 86400000);

  if (dayDiff === 1) return `昨天 ${formatTime(d)}`;
  if (dayDiff === 2) return `前天 ${formatTime(d)}`;
  if (dayDiff > 2 && dayDiff < 8) return `${dayDiff}天前`;
  if (dayDiff === -1) return `明天 ${formatTime(d)}`;
  if (dayDiff < 0 && dayDiff > -8) return `${Math.abs(dayDiff)}天后`;

  const sameYear = d.getFullYear() === base.getFullYear();
  return formatDate(d, sameYear ? "MM-DD HH:mm" : "YYYY-MM-DD");
}

/** 秒数 → "3分12秒" / "1小时4分"（任务耗时展示） */
export function formatDuration(seconds: NumericInput): string {
  const n = toNumber(seconds);
  if (n === null || n < 0) return EMPTY_PLACEHOLDER;
  const total = Math.round(n);
  if (total < 60) return `${total}秒`;
  if (total < 3600) {
    const m = Math.floor(total / 60);
    const s = total % 60;
    return s ? `${m}分${s}秒` : `${m}分`;
  }
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return m ? `${h}小时${m}分` : `${h}小时`;
}

/* ------------------------------------------------------------------ *
 * 其它
 * ------------------------------------------------------------------ */

/** 截断长文本，保留尾部省略号（标题、SKU 用 CSS truncate 更好，此函数用于纯文本场景） */
export function truncate(value: string | null | undefined, max = 40): string {
  if (!value) return EMPTY_PLACEHOLDER;
  return value.length > max ? `${value.slice(0, max)}…` : value;
}
