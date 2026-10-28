// design-system §5.14 空状态 / 错误状态 · §4.2 圆角（图标容器 20px）
// 空状态：图标容器 88px bg-canvas-inset，图标 32px text-fg-disabled stroke 1.5，
// 标题 15/22 600，描述 13/20 muted 最大宽 320px，主操作 mt-5，次操作 mt-2。

import type { ReactNode } from "react";
import { AlertCircle, AlertTriangle, PackageOpen } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

/* ------------------------------------------------------------------ *
 * EmptyState
 * ------------------------------------------------------------------ */

export interface EmptyStateAction {
  label: string;
  onClick: () => void;
  icon?: ReactNode;
  loading?: boolean;
}

export interface EmptyStateProps {
  /** 线性图标，32px（PackageOpen / SearchX / ListChecks / Lock …） */
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  /** 主操作（primary 或 secondary） */
  action?: EmptyStateAction;
  /** 次要操作（文字按钮，text-brand-700） */
  secondaryAction?: EmptyStateAction;
  /** 自定义图标容器底色（错误态用 danger-50） */
  tone?: "default" | "danger";
  className?: string;
  /** 最小高度，列表区 280px */
  minHeight?: number | string;
  children?: ReactNode;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  secondaryAction,
  tone = "default",
  className,
  minHeight = 280,
  children,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center px-6 py-10 text-center",
        className
      )}
      style={{ minHeight }}
    >
      <div
        className={cn(
          "grid size-[88px] place-items-center rounded-2xl",
          tone === "danger" ? "bg-danger-50" : "bg-canvas-inset"
        )}
      >
        <span
          className={cn(
            "grid place-items-center [&_svg]:size-8 [&_svg]:stroke-[1.5]",
            tone === "danger" ? "text-danger-600" : "text-fg-disabled"
          )}
        >
          {icon ?? <PackageOpen strokeWidth={1.5} />}
        </span>
      </div>
      <p className="mt-4 text-[15px] font-semibold leading-[22px] text-fg">{title}</p>
      {description && (
        <p className="mt-1.5 max-w-[320px] text-[13px] leading-5 text-fg-muted">{description}</p>
      )}
      {children}
      {action && (
        <Button
          className="mt-5"
          variant="primary"
          onClick={action.onClick}
          loading={action.loading}
          icon={action.icon}
        >
          {action.label}
        </Button>
      )}
      {secondaryAction && (
        <button
          type="button"
          onClick={secondaryAction.onClick}
          className="mt-2 rounded-sm px-2 py-1 text-[13px] font-medium text-brand-700 transition-colors duration-fast hover:bg-brand-50"
        >
          {secondaryAction.label}
        </button>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * ErrorState（区域级错误：替换列表/卡片区）
 * 图标容器 bg-danger-50，AlertTriangle 32px danger-600，标题「加载失败」，
 * 描述一句话 + 错误码（12px mono muted），操作 重试(primary) / 查看日志(ghost)。
 * ------------------------------------------------------------------ */

export interface ErrorStateProps {
  title?: ReactNode;
  /** 一句人话 */
  description?: ReactNode;
  /** 错误码，如 ERR_NETWORK_500 / RAW code（等宽小字） */
  errorCode?: string | null;
  onRetry?: () => void;
  retryLabel?: string;
  retrying?: boolean;
  onSecondary?: () => void;
  secondaryLabel?: string;
  className?: string;
  minHeight?: number | string;
}

export function ErrorState({
  title = "加载失败",
  description = "请求没有成功，请检查本地服务后重试。",
  errorCode,
  onRetry,
  retryLabel = "重试",
  retrying = false,
  onSecondary,
  secondaryLabel = "查看日志",
  className,
  minHeight = 280,
}: ErrorStateProps) {
  return (
    <div
      className={cn("flex flex-col items-center justify-center px-6 py-10 text-center", className)}
      style={{ minHeight }}
      role="alert"
    >
      <div className="grid size-[88px] place-items-center rounded-2xl bg-danger-50">
        <AlertTriangle className="size-8 text-danger-600" strokeWidth={1.5} />
      </div>
      <p className="mt-4 text-[15px] font-semibold leading-[22px] text-fg">{title}</p>
      {description && (
        <p className="mt-1.5 max-w-[320px] text-[13px] leading-5 text-fg-muted">{description}</p>
      )}
      {errorCode && <p className="mt-2 font-mono text-xs text-fg-muted">{errorCode}</p>}
      {(onRetry || onSecondary) && (
        <div className="mt-5 flex items-center gap-2">
          {onRetry && (
            <Button variant="primary" onClick={onRetry} loading={retrying}>
              {retryLabel}
            </Button>
          )}
          {onSecondary && (
            <Button variant="ghost" onClick={onSecondary}>
              {secondaryLabel}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 行内错误条（§5.14 行内错误条）
 * border danger-600/20 + bg-danger-50，px-3 py-2.5，13/20 danger-700，
 * 左侧 AlertCircle 16px mt-0.5，右侧「重试」文字按钮。
 * ------------------------------------------------------------------ */

export interface InlineErrorProps {
  children: ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
}

export function InlineError({ children, onRetry, retryLabel = "重试", className }: InlineErrorProps) {
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-2 rounded-md border border-danger-600/20 bg-danger-50 px-3 py-2.5",
        "text-[13px] leading-5 text-danger-700",
        className
      )}
    >
      <AlertCircle aria-hidden className="mt-0.5 size-4 shrink-0" strokeWidth={2} />
      <p className="min-w-0 flex-1">{children}</p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="shrink-0 rounded-sm px-1 text-[13px] font-medium text-danger-700 transition-colors duration-fast hover:bg-danger-100"
        >
          {retryLabel}
        </button>
      )}
    </div>
  );
}
