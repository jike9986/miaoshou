// design-system §5.1 图标按钮 · §5.0 点击热区（最小 28×28，触屏 44×44）
// size-7（28px）/ size-9（36px），rounded-md，grid place-items-center，图标 16px，不加边框。

import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

export const iconButtonVariants = cva(
  [
    "relative grid shrink-0 place-items-center",
    "transition-[background-color,color,box-shadow,transform]",
    "duration-fast ease-in-out active:scale-[0.98] active:duration-instant",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32",
    "focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
    "disabled:pointer-events-none disabled:opacity-50",
    "[&_svg]:shrink-0",
  ].join(" "),
  {
    variants: {
      variant: {
        ghost: "bg-transparent text-fg-secondary hover:bg-surface-active hover:text-fg active:bg-border-subtle",
        secondary:
          "border border-border bg-surface text-fg shadow-1 hover:border-border-strong hover:bg-surface-hover active:bg-surface-active",
        danger:
          "bg-transparent text-danger-600 hover:bg-danger-50 hover:text-danger-700 active:bg-danger-100",
      },
      size: {
        sm: "size-7 rounded-sm [&_svg]:size-3.5",
        md: "size-9 rounded-md [&_svg]:size-4",
        lg: "size-11 rounded-md [&_svg]:size-[18px]",
      },
    },
    defaultVariants: { variant: "ghost", size: "sm" },
  }
);

export interface IconButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children" | "aria-label">,
    VariantProps<typeof iconButtonVariants> {
  /** 必填：图标按钮没有可见文字，必须给可访问名 */
  label: string;
  icon: ReactNode;
  /** 显示原生 title 提示（无 Tooltip 组件时的兜底） */
  title?: string;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { className, variant, size, label, icon, title, type = "button", ...props },
  ref
) {
  return (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      title={title ?? label}
      className={cn(iconButtonVariants({ variant, size }), className)}
      {...props}
    >
      {icon}
    </button>
  );
});
