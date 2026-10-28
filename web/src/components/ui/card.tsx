// design-system §5.7 卡片 · §4.2 圆角（卡片 12px） · §4.3 阴影（默认 shadow-1，hover shadow-2）
// 内边距 16px（标准）/ 12px（紧凑）；卡片 hover 仅「可点击」卡片：-translate-y-0.5，160ms out。

import { forwardRef, type ElementType, type HTMLAttributes, type ReactNode } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

export const cardVariants = cva(
  [
    "rounded-lg border border-border bg-surface shadow-1",
    "transition-[transform,box-shadow,border-color] duration-[160ms] ease-out",
  ].join(" "),
  {
    variants: {
      padding: {
        none: "p-0",
        compact: "p-3",
        normal: "p-4",
      },
      /** 可点击卡片才有 hover 位移与投影加深（§5.7） */
      interactive: {
        true: [
          "cursor-pointer",
          "hover:-translate-y-0.5 hover:border-border-strong hover:shadow-2",
          "focus-within:ring-2 focus-within:ring-brand-600/25",
        ].join(" "),
        false: "",
      },
      selected: {
        true: "border-brand-600 ring-1 ring-brand-600 bg-brand-50/40",
        false: "",
      },
      /** 禁用/失败态整卡降透明度（§5.7） */
      muted: { true: "opacity-60", false: "" },
    },
    defaultVariants: { padding: "normal", interactive: false, selected: false, muted: false },
  }
);

export interface CardProps extends HTMLAttributes<HTMLElement>, VariantProps<typeof cardVariants> {
  /** 渲染为 section/article/li（语义化） */
  as?: "div" | "section" | "article" | "li";
}

export const Card = forwardRef<HTMLElement, CardProps>(function Card(
  { className, padding, interactive, selected, muted, as = "div", ...props },
  ref
) {
  // 多态渲染：ElementType 层面的联合类型会让 ref/属性收敛成交集，这里显式放宽到 ElementType
  const Component = as as ElementType;
  return (
    <Component
      ref={ref}
      data-selected={selected ? "true" : undefined}
      className={cn(cardVariants({ padding, interactive, selected, muted }), className)}
      {...props}
    />
  );
});

/* ------------------------------------------------------------------ *
 * 卡片内分区（标题 / 描述 / 内容 / 底部）
 * ------------------------------------------------------------------ */

export function CardHeader({ className, children, action }: { className?: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className={cn("flex items-start justify-between gap-3", className)}>
      <div className="min-w-0 flex-1">{children}</div>
      {action}
    </div>
  );
}

export function CardTitle({ className, children }: { className?: string; children: ReactNode }) {
  return <h3 className={cn("truncate text-h3 text-fg", className)}>{children}</h3>;
}

export function CardDescription({ className, children }: { className?: string; children: ReactNode }) {
  return <p className={cn("mt-1 text-sm text-fg-muted", className)}>{children}</p>;
}

export function CardContent({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn("mt-3", className)}>{children}</div>;
}

export function CardFooter({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div className={cn("mt-3 flex items-center justify-between border-t border-border-subtle pt-3", className)}>
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * KPI 指标卡（§5.7：label 13/20 muted，value 28/36 600 num，delta 12/18 num）
 * ------------------------------------------------------------------ */

export interface KpiCardProps {
  label: ReactNode;
  value: ReactNode;
  /** 涨跌：正数成功色 + ArrowUpRight，负数危险色 + ArrowDownRight（不只靠颜色） */
  delta?: number | null;
  deltaSuffix?: string;
  hint?: ReactNode;
  icon?: ReactNode;
  className?: string;
}

export function KpiCard({ label, value, delta, deltaSuffix = "", hint, icon, className }: KpiCardProps) {
  const positive = typeof delta === "number" && delta > 0;
  const negative = typeof delta === "number" && delta < 0;
  return (
    <Card padding="normal" className={className}>
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm leading-5 text-fg-muted">{label}</p>
        {icon && <span className="text-fg-muted [&_svg]:size-4">{icon}</span>}
      </div>
      <p className="mt-1 num text-[28px] font-semibold leading-9 tracking-[-0.01em] text-fg">{value}</p>
      {(typeof delta === "number" || hint) && (
        <div className="mt-2 flex items-center gap-1.5 text-xs leading-[18px]">
          {typeof delta === "number" && (
            <span
              className={cn(
                "num inline-flex items-center gap-1",
                positive && "text-success-600",
                negative && "text-danger-600",
                !positive && !negative && "text-fg-muted"
              )}
            >
              {positive ? "+" : negative ? "-" : ""}
              {Math.abs(delta).toFixed(1)}%{deltaSuffix}
            </span>
          )}
          {hint && <span className="text-fg-muted">{hint}</span>}
        </div>
      )}
    </Card>
  );
}
