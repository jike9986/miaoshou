// design-system §4.4 边框与分割线（水平 h-px bg-border-subtle，垂直 w-px）
// §5.15 其它基础件：进度条、键盘按键（Kbd 在 select.tsx 中导出）

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ *
 * Separator
 * ------------------------------------------------------------------ */

export interface SeparatorProps {
  orientation?: "horizontal" | "vertical";
  /** 覆盖线色（默认 border-subtle） */
  className?: string;
  /** 通栏（默认）或带缩进 */
  inset?: boolean;
}

export function Separator({ orientation = "horizontal", className, inset = false }: SeparatorProps) {
  if (orientation === "vertical") {
    return (
      <span
        role="separator"
        aria-orientation="vertical"
        className={cn("inline-block w-px self-stretch bg-border-subtle", inset && "my-2", className)}
      />
    );
  }
  return (
    <span
      role="separator"
      aria-orientation="horizontal"
      className={cn("block h-px w-full bg-border-subtle", inset && "my-2", className)}
    />
  );
}

/* ------------------------------------------------------------------ *
 * ScrollArea（统一滚动容器：内容区唯一滚动，§7.1）
 * ------------------------------------------------------------------ */

export interface ScrollAreaProps {
  children: ReactNode;
  className?: string;
  /** 最大高度（如 calc(100vh - 220px)） */
  maxHeight?: string;
  /** 水平滚动（表格宽表） */
  horizontal?: boolean;
  /** 吸顶/吸底留白 */
  padding?: string;
  /** 无障碍标签 */
  label?: string;
  onScroll?: React.UIEventHandler<HTMLDivElement>;
}

export function ScrollArea({
  children,
  className,
  maxHeight,
  horizontal = false,
  padding,
  label,
  onScroll,
}: ScrollAreaProps) {
  return (
    <div
      role="region"
      aria-label={label}
      onScroll={onScroll}
      style={{ maxHeight }}
      className={cn(
        "min-h-0",
        horizontal ? "overflow-auto" : "overflow-y-auto overflow-x-hidden",
        padding,
        className
      )}
    >
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Progress（§5.15：高 6px，rounded-full bg-canvas-inset，填充 bg-brand-600，
 * 宽度过渡 240ms ease-out；不确定态 30% 宽条纹平移 1.2s）
 * ------------------------------------------------------------------ */

export interface ProgressProps {
  /** 0–100 */
  value?: number;
  /** 不确定进度 */
  indeterminate?: boolean;
  className?: string;
  /** 填充色（如失败用 danger） */
  tone?: "brand" | "success" | "danger" | "warning";
  label?: string;
  showValue?: boolean;
}

const PROGRESS_TONE = {
  brand: "bg-brand-600",
  success: "bg-success-500",
  danger: "bg-danger-500",
  warning: "bg-warning-500",
} as const;

export function Progress({
  value,
  indeterminate = false,
  className,
  tone = "brand",
  label,
  showValue = false,
}: ProgressProps) {
  const clamped = typeof value === "number" ? Math.max(0, Math.min(100, value)) : 0;
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      {(label || showValue) && (
        <div className="flex items-center justify-between text-xs text-fg-muted">
          {label && <span>{label}</span>}
          {showValue && !indeterminate && <span className="num">{Math.round(clamped)}%</span>}
        </div>
      )}
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={indeterminate ? undefined : Math.round(clamped)}
        className="h-1.5 w-full overflow-hidden rounded-full bg-canvas-inset"
      >
        {indeterminate ? (
          <div
            className={cn("h-full w-[30%] rounded-full", PROGRESS_TONE[tone])}
            style={{ animation: "progress-slide 1.2s ease-in-out infinite" }}
          />
        ) : (
          <div
            className={cn("h-full rounded-full transition-[width] duration-medium ease-out", PROGRESS_TONE[tone])}
            style={{ width: `${clamped}%` }}
          />
        )}
      </div>
    </div>
  );
}
