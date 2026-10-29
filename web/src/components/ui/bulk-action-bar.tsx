// design-system §5.8 批量操作栏（选中 ≥1 时出现）
// fixed bottom-6 left-1/2 -translate-x-1/2 z-40，h-12 rounded-lg bg-inverse px-3 text-white shadow-3
// 进入：y +8 → 0 + opacity + scale .98 → 1，200ms ease-out；退出 140ms ease-in。
// 内容：已选 N 项（13px）+ 竖分割线（white/15）+ 最多四个操作 + 更多菜单 + 关闭。
// 禁止超过 5 个操作，超出收进「更多」。

import type { ReactNode } from "react";
import { MoreHorizontal, X } from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";
import { ease } from "@/lib/motion";

export interface BulkAction {
  label: string;
  onClick: () => void;
  icon?: ReactNode;
  /** 危险操作（删除 / 下架）hover 变 danger 底 */
  danger?: boolean;
  disabled?: boolean;
}

export interface BulkActionBarProps {
  /** 选中数量；为 0 时不渲染 */
  count: number;
  /** 总数量（显示「已选 12 / 共 30 项」） */
  total?: number;
  /** 单位，默认「项」 */
  unit?: string;
  /** 批量操作；超出四个时收进「更多」 */
  actions?: BulkAction[];
  onClear?: () => void;
  className?: string;
  /** 自定义左侧内容 */
  children?: ReactNode;
}

const MAX_ACTIONS = 5;

export function BulkActionBar({
  count,
  total,
  unit = "项",
  actions = [],
  onClear,
  className,
  children,
}: BulkActionBarProps) {
  const reduced = useReducedMotion();
  const visibleCount = actions.length > MAX_ACTIONS ? MAX_ACTIONS - 1 : MAX_ACTIONS;
  const visibleActions = actions.slice(0, visibleCount);
  const overflowActions = actions.slice(visibleCount);

  return (
    <AnimatePresence>
      {count > 0 && (
        <motion.div
          role="toolbar"
          aria-label="批量操作"
          initial={reduced ? { opacity: 0 } : { opacity: 0, y: 8, scale: 0.98 }}
          animate={reduced ? { opacity: 1 } : { opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 4, transition: { duration: 0.14, ease: ease.in } }}
          transition={{ duration: 0.2, ease: ease.out }}
          className={cn(
            "fixed bottom-6 left-1/2 z-40 flex h-12 max-w-[calc(100vw-1.5rem)] -translate-x-1/2 items-center gap-3 overflow-x-auto",
            "rounded-lg bg-inverse px-3 text-white shadow-3",
            className
          )}
        >
          <span className="num shrink-0 text-[13px] font-medium leading-5">
            已选 {count}
            {typeof total === "number" ? ` / 共 ${total}` : ""} {unit}
          </span>
          <span aria-hidden className="h-5 w-px shrink-0 bg-white/15" />
          {children}
          <div className="flex items-center gap-1">
            {visibleActions.map((action) => (
              <button
                key={action.label}
                type="button"
                onClick={action.onClick}
                disabled={action.disabled}
                className={cn(
                  "inline-flex h-8 items-center gap-1.5 rounded-sm px-2.5 text-[13px] font-medium text-white/90",
                  "transition-colors duration-fast ease-in-out",
                  "hover:bg-white/10 hover:text-white",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/40",
                  "disabled:pointer-events-none disabled:opacity-50",
                  action.danger && "hover:bg-danger-600 hover:text-white"
                )}
              >
                {action.icon}
                {action.label}
              </button>
            ))}
            {overflowActions.length > 0 && (
              <details className="relative shrink-0">
                <summary
                  aria-label="更多批量操作"
                  title="更多批量操作"
                  className="grid size-8 cursor-pointer list-none place-items-center rounded-sm text-white/90 transition-colors hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/40"
                >
                  <MoreHorizontal className="size-4" />
                </summary>
                <div className="fixed bottom-16 right-3 z-50 min-w-44 rounded-md border border-border bg-surface p-1 text-fg shadow-3">
                  {overflowActions.map((action) => (
                    <button
                      key={action.label}
                      type="button"
                      onClick={(event) => {
                        action.onClick();
                        event.currentTarget.closest("details")?.removeAttribute("open");
                      }}
                      disabled={action.disabled}
                      className={cn(
                        "flex h-8 w-full items-center gap-2 rounded-sm px-2 text-left text-[13px] font-medium",
                        "transition-colors hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500",
                        "disabled:pointer-events-none disabled:opacity-50",
                        action.danger && "text-danger-600 hover:bg-danger-50"
                      )}
                    >
                      {action.icon}
                      {action.label}
                    </button>
                  ))}
                </div>
              </details>
            )}
          </div>
          {onClear && (
            <button
              type="button"
              aria-label="取消选择"
              onClick={onClear}
              className="grid size-8 shrink-0 place-items-center rounded-sm text-white/70 transition-colors duration-fast hover:bg-white/10 hover:text-white"
            >
              <X className="size-4" strokeWidth={2} />
            </button>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
