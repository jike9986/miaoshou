// api.md §8 发布 · §9 定时发布 — 请求构造/校验常量
// 仅放常量与纯函数，供业务页与表单校验复用（与后端约束逐条对齐）。

import type { PublishItem, ScheduleFrequency } from "@/lib/types";

/** 上限（api.md 各接口约束） */
export const LIMITS = {
  pageSize: { min: 1, max: 100, default: 20 },
  collectBoxPageSize: { min: 1, max: 20, default: 20 },
  warehousePageSize: { min: 1, max: 100, default: 24 },
  warehouseBatch: 200,
  imageBatch: 200,
  publishBatch: 200,
  wordsPageSize: { min: 1, max: 500, default: 200 },
  wordsTotal: 500,
  titleLength: { min: 25, max: 255 },
  groupNameLength: 64,
  scheduleNameLength: 120,
  schedulePerRun: { min: 1, max: 200 },
  scheduleRangeDays: 180,
  searchMaxLength: 64,
} as const;

/** 站点 → 语言（api.md §2 响应里的 siteLanguages 缺失时兜底） */
export const SITE_LANGUAGES: Record<string, string[]> = {
  MY: ["ms", "en"],
  SG: ["en", "zh"],
  PH: ["en"],
  TH: ["th"],
  VN: ["vi"],
  ID: ["id"],
};

export const LANGUAGE_NAMES: Record<string, string> = {
  ms: "马来语",
  en: "英语",
  zh: "中文",
  th: "泰语",
  vi: "越南语",
  id: "印尼语",
};

/** 定时频率选项（api.md §9.2） */
export const SCHEDULE_FREQUENCY_OPTIONS: Array<{ value: ScheduleFrequency; label: string }> = [
  { value: "daily", label: "每天" },
  { value: "weekly", label: "每周" },
  { value: "monthly", label: "每月" },
  { value: "range", label: "日期区间" },
];

/** 图片任务状态文案与徽标色（api.md §7.2 五态） */
export const IMAGE_STATUS_META: Record<
  string,
  { label: string; tone: "neutral" | "warning" | "success" | "danger" | "info" }
> = {
  queued: { label: "排队中", tone: "neutral" },
  submitted: { label: "处理中", tone: "info" },
  ready: { label: "已完成", tone: "success" },
  unknown: { label: "结果未确认", tone: "warning" },
  failed: { label: "失败", tone: "danger" },
};

/** 发布记录状态文案（api.md §8.2） */
export const PUBLISH_STATUS_META: Record<
  string,
  { label: string; tone: "neutral" | "warning" | "success" | "danger" | "info" }
> = {
  processing: { label: "处理中", tone: "warning" },
  success: { label: "发布成功", tone: "success" },
  fail: { label: "发布失败", tone: "danger" },
  cancel: { label: "已取消", tone: "neutral" },
};

/** 仓库商品状态文案（api.md §4.1 status） */
export const PRODUCT_STATUS_META: Record<
  string,
  { label: string; tone: "neutral" | "warning" | "success" | "danger" | "info" }
> = {
  ready: { label: "待上架", tone: "neutral" },
  claimed: { label: "已认领", tone: "warning" },
  published: { label: "已上架", tone: "success" },
  archived: { label: "已归档", tone: "neutral" },
};

/** 构造发布 items（api.md §8.1：每项给 warehouseId 或 detailId+shopId） */
export function toPublishItems(
  refs: Array<{ warehouseId?: string | null; detailId?: string | null; shopId?: string | null }>
): PublishItem[] {
  const items: PublishItem[] = [];
  for (const ref of refs) {
    if (ref.warehouseId) {
      items.push({ warehouseId: ref.warehouseId });
    } else if (ref.detailId && ref.shopId) {
      items.push({ detailId: ref.detailId, shopId: ref.shopId });
    }
  }
  return items;
}

/** 标题长度校验（api.md §4.3：25–255 字符，按字符计） */
export function validateTitle(title: string): string | null {
  const length = Array.from(title.trim()).length;
  if (length < LIMITS.titleLength.min) return `标题至少 ${LIMITS.titleLength.min} 个字符（当前 ${length}）`;
  if (length > LIMITS.titleLength.max) return `标题最多 ${LIMITS.titleLength.max} 个字符（当前 ${length}）`;
  return null;
}

/** 语言与站点是否匹配（api.md §8.1 SITE_LANGUAGE_MISMATCH） */
export function isLanguageMismatch(site: string, language: string, siteLanguages?: Record<string, string[]>): boolean {
  const allowed = siteLanguages?.[site] ?? SITE_LANGUAGES[site];
  if (!allowed) return false;
  return !allowed.includes(language);
}
