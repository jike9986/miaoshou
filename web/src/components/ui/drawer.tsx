// design-system §5.13 抽屉(Drawer / Sheet) · §6.2（进入 x 24→0 + opacity 240ms out；退出 x 0→16 180ms in）
// 面板：right-0 top-0 h-full bg-elevated border-l shadow-3 rounded-l-xl；头部 56，内容 p-6，底部 64。
// 打开时不压缩主内容宽度，只覆盖；宽度切换不动画。

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
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
  const onKeyDown = useTabTrap(panelRef);

  useFocusTrap(open, panelRef);

  const handleClose = useCallback(() => {
    onOpenChange(false);
  }, [onOpenChange]);

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

  // 不用 AnimatePresence：framer-motion 的退出动画在 CDP/生产环境下
  // 有时会卡住不卸载 DOM，导致抽屉关不掉。也不用 setTimeout 延迟卸载：
  // React 18 的并发模式下 setTimeout 回调可能被丢弃。
  // 直接用 open 控制渲染，CSS 过渡靠 transition 属性。
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (open) {
      // 用 setTimeout 让 CSS 过渡能播放（10ms 足够浏览器渲染一帧）
      const timer = setTimeout(() => setVisible(true), 10);
      return () => clearTimeout(timer);
    } else {
      setVisible(false);
    }
  }, [open]);

  // 不用 createPortal：React 18 的并发模式下 portal 的事件委托有时不工作，
  // 导致按钮点击不触发 onClick。直接渲染到 React 树里，用 fixed 定位覆盖。
  if (typeof document === "undefined" || !open) return null;

  return (
    <div
      className={cn(
        "fixed inset-0 z-[50] transition-opacity duration-200",
        visible ? "opacity-100" : "pointer-events-none opacity-0"
      )}
      role="dialog"
      aria-modal="true"
      aria-labelledby={title ? titleId : undefined}
      aria-describedby={description ? descId : undefined}
    >
      {/* 遮罩 */}
      <div
        className="absolute inset-0 bg-overlay/45 backdrop-blur-[2px]"
        onClick={closeOnOverlay ? handleClose : undefined}
      />
      {/* 面板 */}
      <div
        ref={panelRef}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className={cn(
          "absolute right-0 top-0 flex h-full flex-col",
          "rounded-l-xl border-l border-border bg-elevated shadow-3",
          "max-w-[calc(100vw-48px)] focus:outline-none",
          "transition-transform duration-200 ease-out",
          visible ? "translate-x-0" : "translate-x-6",
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
      </div>
    </div>
  );
}


