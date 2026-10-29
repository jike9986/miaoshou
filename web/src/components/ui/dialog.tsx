// design-system §5.12 弹窗(Dialog) · §4.5 层级(overlay 50 / modal 60) · §6.2 动效
// 遮罩与内容只做进入动画；关闭立即卸载，避免透明退出层残留并阻挡页面。
// 焦点锁定在弹窗内、Esc 关闭、危险确认默认聚焦「取消」、表单类弹窗点遮罩不关闭。

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { motion, useReducedMotion } from "framer-motion";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { ease, overlayFade, scaleIn } from "@/lib/motion";
import { IconButton } from "@/components/ui/icon-button";
import { Button } from "@/components/ui/button";

/* ------------------------------------------------------------------ *
 * 通用浮层基础：焦点锁定 + Esc + 滚动锁定
 * ------------------------------------------------------------------ */

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

export function useFocusTrap(
  active: boolean,
  containerRef: React.RefObject<HTMLElement>,
  initialRef?: React.RefObject<HTMLElement>,
  initialSelector?: string
) {
  useEffect(() => {
    if (!active) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    // 打开时聚焦第一个可交互元素，或指定的初始焦点
    const target =
      initialRef?.current ??
      (initialSelector ? containerRef.current?.querySelector<HTMLElement>(initialSelector) : null) ??
      containerRef.current?.querySelector<HTMLElement>(`${FOCUSABLE}:not([data-focus-skip])`) ??
      null;
    target?.focus({ preventScroll: true });

    // 锁定背景滚动
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    return () => {
      document.body.style.overflow = originalOverflow;
      // 延迟恢复焦点：等退出动画完成后再还给之前的元素。
      // 立即恢复会在动画期间把焦点还给触发按钮，导致 onClick 重新打开抽屉。
      requestAnimationFrame(() => {
        previous?.focus({ preventScroll: true });
      });
    };
  }, [active, containerRef, initialRef, initialSelector]);
}

export function useTabTrap(containerRef: React.RefObject<HTMLElement>) {
  return useCallback(
    (event: ReactKeyboardEvent<HTMLElement>) => {
      if (event.key !== "Tab") return;
      const nodes = containerRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE);
      if (!nodes || nodes.length === 0) return;
      const list = Array.from(nodes).filter((node) => node.offsetParent !== null);
      if (list.length === 0) return;
      const first = list[0];
      const last = list[list.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [containerRef]
  );
}

/* ------------------------------------------------------------------ *
 * Dialog
 * ------------------------------------------------------------------ */

export type DialogSize = "sm" | "md" | "lg" | "xl";

const DIALOG_WIDTH: Record<DialogSize, string> = {
  sm: "max-w-[400px]",
  md: "max-w-[480px]",
  lg: "max-w-[640px]",
  xl: "max-w-[880px]",
};

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  size?: DialogSize;
  title?: ReactNode;
  description?: ReactNode;
  /** 头部右侧关闭按钮，默认显示 */
  showClose?: boolean;
  children?: ReactNode;
  /** 底部操作区（主操作在右） */
  footer?: ReactNode;
  /** 表单类弹窗传 false：点遮罩不关闭，防误触丢数据（§5.12） */
  closeOnOverlay?: boolean;
  /** 危险确认默认聚焦「取消」：传 "cancel" 让取消按钮获得初始焦点 */
  initialFocus?: "first" | "cancel" | "none";
  className?: string;
  contentClassName?: string;
}

export function Dialog({
  open,
  onOpenChange,
  size = "md",
  title,
  description,
  showClose = true,
  children,
  footer,
  closeOnOverlay = true,
  initialFocus = "first",
  className,
  contentClassName,
}: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descId = useId();
  const reduced = useReducedMotion();
  const onKeyDown = useTabTrap(panelRef);

  useFocusTrap(open, panelRef, undefined, initialFocus === "cancel" ? "[data-dialog-cancel]" : undefined);

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
        show: { opacity: 1, transition: { duration: 0.2, ease: ease.out } },
      }
    : scaleIn;

  return createPortal(
    open ? (
        <div
          className="fixed inset-0 z-[50]"
          role="dialog"
          aria-modal="true"
          aria-labelledby={title ? titleId : undefined}
          aria-describedby={description ? descId : undefined}
        >
          <motion.div
            className="absolute inset-0 bg-overlay backdrop-blur-[2px]"
            variants={overlayFade}
            initial="hidden"
            animate="show"
            onClick={closeOnOverlay ? handleClose : undefined}
          />
          <div className="pointer-events-none absolute inset-0 grid place-items-center p-6">
            <motion.div
              ref={panelRef}
              tabIndex={-1}
              onKeyDown={onKeyDown}
              variants={variants}
              initial="hidden"
              animate="show"
              exit="exit"
              className={cn(
                "pointer-events-auto relative flex max-h-[85vh] w-full flex-col",
                "rounded-xl border border-border bg-elevated shadow-3",
                "focus:outline-none",
                DIALOG_WIDTH[size],
                className
              )}
            >
              {(title || showClose) && (
                <div className="relative px-6 pb-0 pt-5">
                  {title && (
                    <h2 id={titleId} className="pr-8 text-h3 text-fg">
                      {title}
                    </h2>
                  )}
                  {description && (
                    <p id={descId} className="mt-1.5 text-sm leading-5 text-fg-muted">
                      {description}
                    </p>
                  )}
                  {showClose && (
                    <IconButton
                      label="关闭"
                      size="sm"
                      icon={<X />}
                      onClick={handleClose}
                      className="absolute right-3 top-3"
                    />
                  )}
                </div>
              )}
              <div className={cn("overflow-y-auto px-6 py-4", contentClassName)}>{children}</div>
              {footer && (
                <div className="flex justify-end gap-2 border-t border-border-subtle px-6 py-4">
                  {footer}
                </div>
              )}
            </motion.div>
          </div>
        </div>
      ) : null,
    document.body
  );
}

/* ------------------------------------------------------------------ *
 * 便捷封装
 * ------------------------------------------------------------------ */

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  /** 危险确认必须写清影响对象与数量（§5.12） */
  children?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  loading?: boolean;
  onConfirm: () => void;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  confirmLabel = "确认",
  cancelLabel = "取消",
  danger = false,
  loading = false,
  onConfirm,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="sm"
      title={title}
      description={description}
      closeOnOverlay
      initialFocus="cancel"
      footer={
        <>
          {/* 危险确认默认聚焦「取消」：首个按钮即取消，符合 §5.12 的焦点要求 */}
          <Button data-dialog-cancel variant="secondary" onClick={() => onOpenChange(false)}>
            {cancelLabel}
          </Button>
          <Button variant={danger ? "danger" : "primary"} loading={loading} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      {children}
    </Dialog>
  );
}

/** 受控开合的轻量 hook（业务页最常用） */
export function useDialogState(initial = false): {
  open: boolean;
  setOpen: (value: boolean) => void;
  show: () => void;
  hide: () => void;
  toggle: () => void;
} {
  const [open, setOpen] = useState(initial);
  return {
    open,
    setOpen,
    show: () => setOpen(true),
    hide: () => setOpen(false),
    toggle: () => setOpen((prev) => !prev),
  };
}
