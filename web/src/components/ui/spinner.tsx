// design-system §5.0 通用状态约定（loading：左侧 spinner 14px，border-2，
// 600ms linear，文字不变、宽度不变）· §6.1 时长
// 单独抽出：`border-current/30` 这类写法在 Tailwind 里对 currentColor 不生效，
// 这里用 color-mix 显式实现「同色 30% 轨道 + 实色旋转头」，语义不变、颜色仍随父级。

import { cn } from "@/lib/utils";

export interface SpinnerProps {
  /** 直径 px：按钮内 14，区块 16，页面级 20 */
  size?: number;
  className?: string;
  label?: string;
}

export function Spinner({ size = 14, className, label }: SpinnerProps) {
  return (
    <span
      role={label ? "status" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={cn("spinner shrink-0", className)}
      style={{ width: size, height: size, borderWidth: size >= 18 ? 2.5 : 2 }}
    />
  );
}
