// design-system §5.1 按钮 · §5.0 通用状态约定 · §6.2 逐场景规格（hover 120ms / press 90ms）
// 尺寸：sm 28px / md 36px / lg 44px；图标 14/16/18；同一屏只允许一个 primary。

import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";
import { Spinner } from "@/components/ui/spinner";

export const buttonVariants = cva(
  [
    "relative inline-flex select-none items-center justify-center gap-2 whitespace-nowrap font-medium",
    "transition-[background-color,border-color,color,box-shadow,transform]",
    "duration-fast ease-in-out active:scale-[0.98] active:duration-instant",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32",
    "focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
    "disabled:pointer-events-none disabled:opacity-50",
    "[&_svg]:shrink-0",
  ].join(" "),
  {
    variants: {
      variant: {
        primary: [
          "bg-brand-600 text-white shadow-1",
          "hover:bg-brand-700 active:bg-brand-800",
          "disabled:bg-brand-600/45 disabled:shadow-none",
        ].join(" "),
        secondary: [
          "border border-border bg-surface text-fg shadow-1",
          "hover:border-border-strong hover:bg-surface-hover",
          "active:bg-surface-active",
        ].join(" "),
        ghost: [
          "bg-transparent text-fg-secondary",
          "hover:bg-surface-active hover:text-fg",
          "active:bg-border-subtle",
        ].join(" "),
        danger: [
          "bg-danger-600 text-white shadow-1",
          "hover:bg-danger-700 active:bg-danger-700",
          "focus-visible:ring-danger-600/30",
          "disabled:bg-danger-600/45 disabled:shadow-none",
        ].join(" "),
      },
      size: {
        sm: "h-7 rounded-sm px-2.5 text-[13px] leading-5 gap-1.5 [&_svg]:size-3.5",
        md: "h-9 rounded-md px-3.5 text-base gap-2 [&_svg]:size-4",
        lg: "h-11 rounded-[10px] px-5 text-base gap-2 [&_svg]:size-[18px]",
      },
      /** 占满父容器（抽屉底部整行主操作） */
      block: { true: "w-full", false: "" },
    },
    defaultVariants: { variant: "primary", size: "md", block: false },
  }
);

export interface ButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children">,
    VariantProps<typeof buttonVariants> {
  /** loading 期间文字不变、宽度不变（min-w 锁死，§5.0） */
  loading?: boolean;
  /** loading 时的文案替换，默认保留原 children */
  loadingText?: string;
  /** 左图标（最多一个，§5.1 不允许两个以上图标） */
  icon?: ReactNode;
  /** 右图标（如 ChevronDown / ArrowRight） */
  iconRight?: ReactNode;
  children?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    className,
    variant,
    size,
    block,
    loading = false,
    loadingText,
    icon,
    iconRight,
    children,
    disabled,
    type = "button",
    ...props
  },
  ref
) {
  const content = loading && loadingText ? loadingText : children;

  return (
    <button
      ref={ref}
      type={type}
      className={cn(buttonVariants({ variant, size, block }), loading && "cursor-progress", className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading ? (
        <Spinner size={14} className="text-current" />
      ) : (
        icon
      )}
      {content}
      {!loading && iconRight}
    </button>
  );
});

export type ButtonVariant = NonNullable<VariantProps<typeof buttonVariants>["variant"]>;
export type ButtonSize = NonNullable<VariantProps<typeof buttonVariants>["size"]>;
