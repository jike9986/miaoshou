// design-system §5.6 标签 / 徽标 / 状态点 · §2.4 状态色（业务状态映射统一在此）
// Badge：h-5 rounded-xs px-1.5 text-[11px] font-medium leading-4。
// 状态点：6px 圆 + 徽标，gap-1.5；「审核中」点使用 status-breathe 1.8s（全应用唯一循环）。

import type { ReactNode } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ *
 * Badge（§5.6）
 * ------------------------------------------------------------------ */

export const badgeVariants = cva(
  "inline-flex items-center gap-1 whitespace-nowrap rounded-xs text-2xs font-medium",
  {
    variants: {
      variant: {
        neutral: "bg-canvas-inset text-fg-secondary",
        brand: "bg-brand-50 text-brand-700",
        success: "bg-success-50 text-success-700",
        warning: "bg-warning-50 text-warning-700",
        danger: "bg-danger-50 text-danger-700",
        info: "bg-info-50 text-info-700",
        /** 反色浮层上的徽标（批量操作栏、标题栏） */
        inverse: "bg-white/15 text-white",
      },
      size: {
        sm: "h-4 px-1",
        md: "h-5 px-1.5",
      },
      /** 描边版（表格内需要更弱的存在感） */
      outline: {
        true: "border border-border bg-transparent",
        false: "",
      },
    },
    defaultVariants: { variant: "neutral", size: "md", outline: false },
  }
);

export type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>["variant"]>;

export interface BadgeProps extends VariantProps<typeof badgeVariants> {
  children: ReactNode;
  icon?: ReactNode;
  className?: string;
  title?: string;
}

export function Badge({ children, icon, variant, size, outline, className, title }: BadgeProps) {
  return (
    <span title={title} className={cn(badgeVariants({ variant, size, outline }), className)}>
      {icon}
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------ *
 * Tag（可移除标签，§5.6 可移除标签：徽标 + 右侧 X 12px，hover:bg-surface-hover）
 * ------------------------------------------------------------------ */

export interface TagProps {
  children: ReactNode;
  onRemove?: () => void;
  removeLabel?: string;
  variant?: BadgeVariant;
  className?: string;
  icon?: ReactNode;
}

export function Tag({ children, onRemove, removeLabel = "移除", variant = "neutral", className, icon }: TagProps) {
  return (
    <span
      className={cn(
        badgeVariants({ variant, size: "md" }),
        "gap-1.5 rounded-xs px-2 py-0.5",
        onRemove && "pr-1",
        className
      )}
    >
      {icon}
      <span className="max-w-[200px] truncate">{children}</span>
      {onRemove && (
        <button
          type="button"
          aria-label={removeLabel}
          onClick={onRemove}
          className="grid size-4 place-items-center rounded-xs opacity-70 transition-[opacity,background-color] duration-fast hover:bg-surface-hover hover:opacity-100"
        >
          <X className="size-3" strokeWidth={2.4} />
        </button>
      )}
    </span>
  );
}

/* ------------------------------------------------------------------ *
 * StatusDot（6px 圆点，可呼吸/旋转）
 * ------------------------------------------------------------------ */

export type StatusTone = "neutral" | "success" | "warning" | "danger" | "info" | "muted";

const DOT_COLOR: Record<StatusTone, string> = {
  neutral: "bg-border-strong",
  muted: "bg-fg-muted",
  success: "bg-success-500",
  warning: "bg-warning-500",
  danger: "bg-danger-500",
  info: "bg-info-500",
};

export interface StatusDotProps {
  tone?: StatusTone;
  /** 呼吸动画：仅「审核中/上架中」允许（§2.4） */
  breathe?: boolean;
  /** 旋转动画：仅「同步中」允许（§2.4） */
  spinning?: boolean;
  size?: 6 | 8;
  className?: string;
  label?: string;
}

export function StatusDot({ tone = "neutral", breathe, spinning, size = 6, className, label }: StatusDotProps) {
  return (
    <span
      role={label ? "img" : undefined}
      aria-label={label}
      className={cn(
        "inline-block shrink-0 rounded-full",
        size === 6 ? "size-1.5" : "size-2",
        DOT_COLOR[tone],
        breathe && "animate-status-breathe",
        spinning && "animate-spin [animation-duration:1.2s]",
        className
      )}
    />
  );
}

/* ------------------------------------------------------------------ *
 * StatusBadge：状态点 + 徽标（业务状态映射，§2.4）
 * 全应用统一，不允许各页面自定义配色。
 * ------------------------------------------------------------------ */

export interface StatusMeta {
  label: string;
  tone: BadgeVariant;
  dot: StatusTone;
  /** 仅「上架中 / 审核中」为 true（呼吸） */
  breathe?: boolean;
  /** 仅「同步中」为 true（旋转） */
  spinning?: boolean;
}

/** 业务状态 → 徽标/点（§2.4 表格逐条对应） */
export function productStatusMeta(status: string): StatusMeta {
  switch (status) {
    case "ready":
      return { label: "待上架", tone: "neutral", dot: "neutral" };
    case "claimed":
      return { label: "已认领", tone: "warning", dot: "warning", breathe: true };
    case "published":
      return { label: "已上架", tone: "success", dot: "success" };
    case "archived":
      return { label: "已下架", tone: "neutral", dot: "muted" };
    case "failed":
      return { label: "失败", tone: "danger", dot: "danger" };
    case "syncing":
      return { label: "同步中", tone: "info", dot: "info", spinning: true };
    default:
      return { label: status || "未知", tone: "neutral", dot: "neutral" };
  }
}

export interface StatusBadgeProps {
  meta: StatusMeta;
  className?: string;
  /** 只显示点（表格紧凑列） */
  dotOnly?: boolean;
}

export function StatusBadge({ meta, className, dotOnly }: StatusBadgeProps) {
  const dot = <StatusDot tone={meta.dot} breathe={meta.breathe} spinning={meta.spinning} label={meta.label} />;
  if (dotOnly) return <span className={cn("inline-flex", className)}>{dot}</span>;
  return (
    <Badge variant={meta.tone} className={cn("gap-1.5", className)}>
      {dot}
      {meta.label}
    </Badge>
  );
}
