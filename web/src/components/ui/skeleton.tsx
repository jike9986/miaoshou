// design-system §5.14 骨架屏 · §4.2 圆角（基础块 3px）
// 背景 linear-gradient(90deg,#EDF1F3 25%,#F6F8F9 37%,#EDF1F3 63%)，400% 宽，
// shimmer 1.6s linear infinite —— 全应用唯一的长循环（§6.2）。
// 实现放在 styles/animations.css 的 .skeleton-shimmer（颜色走 token，不写 hex）。

import type { CSSProperties, ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface SkeletonProps {
  className?: string;
  /** 圆角覆盖（默认 3px：骨架基础块） */
  rounded?: string;
  style?: CSSProperties;
}

/** 基础块 */
export function Skeleton({ className, rounded = "rounded-[3px]", style }: SkeletonProps) {
  return <div aria-hidden className={cn("skeleton-shimmer", rounded, className)} style={style} />;
}

/* ------------------------------------------------------------------ *
 * 文本行：高 12px，宽度 60% / 80% / 40% 交错
 * ------------------------------------------------------------------ */

export interface SkeletonTextProps {
  /** 行数，默认 1 */
  lines?: number;
  className?: string;
  lineClassName?: string;
}

const LINE_WIDTHS = ["w-3/5", "w-4/5", "w-2/5"];

export function SkeletonText({ lines = 1, className, lineClassName }: SkeletonTextProps) {
  return (
    <div className={cn("flex flex-col gap-2", className)} aria-hidden>
      {Array.from({ length: lines }).map((_, index) => (
        <Skeleton key={index} className={cn("h-3", LINE_WIDTHS[index % LINE_WIDTHS.length], lineClassName)} />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 骨架屏容器：总时长 > 3s 时在骨架上方显示「正在加载，请稍候…」（§5.14）
 * ------------------------------------------------------------------ */

export interface SkeletonGroupProps {
  children: ReactNode;
  /** 是否显示「慢加载」提示 */
  slow?: boolean;
  label?: string;
  className?: string;
  /** 供屏幕阅读器 */
  "aria-label"?: string;
}

export function SkeletonGroup({
  children,
  slow = false,
  label = "正在加载，请稍候…",
  className,
  "aria-label": ariaLabel = "加载中",
}: SkeletonGroupProps) {
  return (
    <div className={className} role="status" aria-busy aria-label={ariaLabel}>
      {slow && <p className="mb-2 text-xs text-fg-muted">{label}</p>}
      {children}
      <span className="sr-only">{label}</span>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 商品卡骨架（图片区 aspect-square + 2 行文字 + 1 行价格 40%）
 * ------------------------------------------------------------------ */

export function SkeletonCard({ className }: { className?: string }) {
  return (
    <div className={cn("overflow-hidden rounded-lg border border-border bg-surface shadow-1", className)}>
      <Skeleton className="aspect-square w-full" rounded="rounded-none" />
      <div className="flex flex-col gap-2 p-3">
        <Skeleton className="h-3.5 w-4/5" />
        <Skeleton className="h-3.5 w-3/5" />
        <Skeleton className="mt-1 h-4 w-2/5" />
        <div className="flex items-center gap-3">
          <Skeleton className="h-3 w-14" />
          <Skeleton className="h-3 w-12" />
        </div>
      </div>
    </div>
  );
}

/** 商品卡网格骨架（数量 = 首屏可见数量 + 2） */
export function SkeletonCardGrid({ count = 8, className }: { count?: number; className?: string }) {
  return (
    <div className={cn("grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5", className)}>
      {Array.from({ length: count }).map((_, index) => (
        <SkeletonCard key={index} />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 表格骨架（8 行 × 44px，列宽与真实表一致，避免加载完成跳动）
 * ------------------------------------------------------------------ */

export interface SkeletonTableProps {
  rows?: number;
  /** 每列宽度类名，需与真实表列宽一致 */
  columns?: string[];
  className?: string;
}

export function SkeletonTable({
  rows = 8,
  columns = ["w-10", "flex-1", "w-[120px]", "w-[88px]", "w-[104px]", "w-[96px]"],
  className,
}: SkeletonTableProps) {
  return (
    <div className={cn("overflow-hidden rounded-lg border border-border bg-surface", className)} aria-hidden>
      <div className="flex h-10 items-center gap-3 border-b border-border bg-canvas-inset/70 px-4">
        {columns.map((width, index) => (
          <Skeleton key={index} className={cn("h-3", width)} />
        ))}
      </div>
      {Array.from({ length: rows }).map((_, rowIndex) => (
        <div
          key={rowIndex}
          className="flex h-11 items-center gap-3 border-b border-border-subtle px-4 last:border-0"
        >
          {columns.map((width, colIndex) => (
            <Skeleton
              key={colIndex}
              className={cn("h-3", width)}
              style={colIndex === 1 ? { width: `${70 + ((rowIndex * 7) % 25)}%` } : undefined}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

/** 通用列表行骨架（详情页 / 抽屉内） */
export function SkeletonRows({
  rows = 4,
  className,
  label = "正在加载",
}: {
  rows?: number;
  className?: string;
  label?: string;
}) {
  return (
    <div className={cn("flex flex-col gap-3", className)} role="status" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="flex items-center gap-3" aria-hidden>
          <Skeleton className="size-10" rounded="rounded-sm" />
          <div className="flex-1">
            <SkeletonText lines={2} />
          </div>
        </div>
      ))}
      <span className="sr-only">{label}</span>
    </div>
  );
}
