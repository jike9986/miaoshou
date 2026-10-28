// features/settings —— 标题栏右侧的身份按钮
//
// 它承担两个职责：
//   1. 随时看得见「现在是谁」—— 多个人共用一台机器时，这是最容易搞错的一件事；
//   2. 作为设置页的入口 —— 不在左侧导航里加常驻项（导航是「做事的板块」），
//      身份按钮本来就是「关于我」的位置，设置挂在这里最自然。
//
// 没设置身份时不报警、不拦路：显示内置的「本机」，点进去可以选一个。

import { UserRound } from "lucide-react";
import { cn } from "@/lib/utils";
import { LOCAL_OPERATOR_NAME, useOperatorIdentity } from "@/lib/operator";
import { Tooltip } from "@/components/ui";

export interface OperatorIdentityButtonProps {
  onClick: () => void;
  className?: string;
}

export function OperatorIdentityButton({ onClick, className }: OperatorIdentityButtonProps) {
  const identity = useOperatorIdentity();
  const name = identity?.name ?? LOCAL_OPERATOR_NAME;

  return (
    <Tooltip
      content={
        identity
          ? `当前身份：${name}。点开可切换身份、管理操作员、查看审计日志`
          : `还没设置操作员，改动会记在「${LOCAL_OPERATOR_NAME}」名下。点开可设置身份`
      }
    >
      <button
        type="button"
        onClick={onClick}
        className={cn(
          "no-drag flex h-7 max-w-[180px] items-center gap-1.5 rounded-sm px-2",
          "text-xs text-fg-secondary transition-colors duration-fast",
          "hover:bg-surface-active hover:text-fg",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32",
          className
        )}
      >
        <UserRound aria-hidden className="size-3.5 shrink-0" strokeWidth={1.75} />
        <span className="min-w-0 truncate">{name}</span>
        {!identity && (
          <span className="shrink-0 rounded-xs bg-canvas-inset px-1 text-[10px] leading-4 text-fg-muted">
            未设置
          </span>
        )}
      </button>
    </Tooltip>
  );
}

export default OperatorIdentityButton;
