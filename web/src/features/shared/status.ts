// 业务文案表（@/lib/constants）→ StatusBadge 需要的 StatusMeta。
// 常量表只存 label + tone，状态点的 tone 在这里按语义映射一次，页面不再各自推导。

import type { StatusMeta, StatusTone } from "@/components/ui";
import type { BadgeVariant } from "@/components/ui";
import {
  IMAGE_STATUS_META,
  PRODUCT_STATUS_META,
  PUBLISH_STATUS_META,
} from "@/lib/constants";

const TONE_TO_DOT: Record<string, StatusTone> = {
  neutral: "neutral",
  brand: "neutral",
  success: "success",
  warning: "warning",
  danger: "danger",
  info: "info",
};

interface MetaEntry {
  label: string;
  tone: BadgeVariant;
}

function toStatusMeta(entry: MetaEntry | undefined, fallbackLabel: string): StatusMeta {
  if (!entry) return { label: fallbackLabel || "未知", tone: "neutral", dot: "neutral" };
  return { label: entry.label, tone: entry.tone, dot: TONE_TO_DOT[entry.tone] ?? "neutral" };
}

/** 仓库商品状态（api.md §4.1 status） */
export function productStatus(status: string): StatusMeta {
  return toStatusMeta(PRODUCT_STATUS_META[status], status);
}

/** 发布记录状态（api.md §8.2） */
export function publishStatus(status: string): StatusMeta {
  const meta = toStatusMeta(PUBLISH_STATUS_META[status], status);
  // 「处理中」允许呼吸（design-system §2.4 唯一允许的循环动效）
  return status === "processing" ? { ...meta, breathe: true } : meta;
}

/** 图片任务状态（api.md §7.2 五态） */
export function imageStatus(status: string): StatusMeta {
  return toStatusMeta(IMAGE_STATUS_META[status], status);
}
