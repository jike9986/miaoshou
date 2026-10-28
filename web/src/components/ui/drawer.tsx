// design-system §5.13 抽屉(Drawer / Sheet) · §6.2（进入 x 24→0 + opacity 240ms out；退出 x 0→16 180ms in）
// 面板：right-0 top-0 h-full bg-elevated border-l shadow-3 rounded-l-xl；头部 56，内容 p-6，底部 64。
// 打开时不压缩主内容宽度，只覆盖；宽度切换不动画。

import { useCallback, useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { drawerRight, ease, overlayFade } from "@/lib/motion";
import { IconButton } from "@/components/ui/icon-button";
import { useFocusTrap, useTabTrap } from "@/components/ui/dialog";

export type DrawerSize = "sm" | "md" | "lg" | "xl";

const DRAWER_WIDTH: Record<DrawerSize, string> = {
  sm: "w-[400px]",
  md: "w-[480px]",
  lg: "w-[640px]",
  xl: "w-[800px]",
};

export interface DrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  size?: DrawerSize;
  title?: ReactNode;
  /** 标题下的副标题（如 SKU / 店铺名） */
  description?: ReactNode;
  /** 头部右侧、关闭按钮左侧的附加操作 */
  headerActions?: ReactNode;
  children?: ReactNode;
  /** 底部固定操作区（高 64px，不滚动） */
  footer?: ReactNode;
  closeOnOverlay?: boolean;
  className?: string;
  contentClassName?: string;
}

export function Drawer({
  open,
  onOpenChange,
  size = "md",
  title,
  description,
  headerActions,
  children,
  footer,
  closeOnOverlay = true,
  className,
  contentClassName,
}: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descId = useId();
  const reduced = useReducedMotion();
  const onKeyDown = useTabTrap(panelRef);

  useFocusTrap(open, panelRef);

  const handleClose = useCallback(() => onOpenChange(false), [onOpenChange]);

  useEffect(() => {
    if (!open) return;
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (event.key === "Escape") {
        event.preventDefault();
        handleClose();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, handleClose]);

  if (typeof document === "undefined") return null;

  const variants = reduced
    ? {
        hidden: { opacity: 0 },
        show: { opacity: 1, transition: { duration: 0.24, ease: ease.out } },
        exit: { opacity: 0, transition: { duration: 0.18, ease: ease.in } },
      }
    : drawerRight;

  return createPortal(
    <AnimatePresence>
      {open && (
        <div
          className="fixed inset-0 z-[50]"
          role="dialog"
          aria-modal="true"
          aria-labelledby={title ? titleId : undefined}
          aria-describedby={description ? descId : undefined}
        >
          <motion.div
            className="absolute inset-0 bg-overlay/45 backdrop-blur-[2px]"
            variants={overlayFade}
            initial="hidden"
            animate="show"
            exit="exit"
            onClick={closeOnOverlay ? handleClose : undefined}
          />
          <motion.div
            ref={panelRef}
            tabIndex={-1}
            onKeyDown={onKeyDown}
            variants={variants}
            initial="hidden"
            animate="show"
            exit="exit"
            className={cn(
              "absolute right-0 top-0 flex h-full flex-col",
              "rounded-l-xl border-l border-border bg-elevated shadow-3",
              "max-w-[calc(100vw-48px)] focus:outline-none",
              DRAWER_WIDTH[size],
              className
            )}
          >
            <div className="flex h-14 shrink-0 items-center gap-3 border-b border-border px-6">
              <div className="min-w-0 flex-1">
                {title && (
                  <h2 id={titleId} className="truncate text-h3 text-fg">
                    {title}
                  </h2>
                )}
                {description && (
                  <p id={descId} className="truncate text-xs text-fg-muted">
                    {description}
                  </p>
                )}
              </div>
              {headerActions}
              <IconButton label="关闭" size="sm" icon={<X />} onClick={handleClose} />
            </div>

            <div className={cn("flex-1 overflow-y-auto p-6", contentClassName)}>{children}</div>

            {footer && (
              <div className="flex h-16 shrink-0 items-center justify-end gap-2 border-t border-border px-6">
                {footer}
              </div>
            )}
          </motion.div>
        </div>
      )}
    </AnimatePresence>,
    document.body
  );
}

/** Sheet 是 Drawer 的别名（同一规格，便于业务侧按语义取名） */
export { Drawer as Sheet };
