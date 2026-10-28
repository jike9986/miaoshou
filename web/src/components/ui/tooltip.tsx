// design-system §5.15 Tooltip —— 底色为 §2.2 中性 900（反色浮层），text-xs px-2 py-1 rounded-sm shadow-2，
// 最大宽 240px，延迟 300ms（表格内 400ms），进入 120ms。纯 CSS + 定时器实现，无额外依赖。
// 颜色走 token：bg-inverse（--surface-inverse = #191F25），不硬编码 hex。

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";
import { ease } from "@/lib/motion";

export type TooltipSide = "top" | "right" | "bottom" | "left";

export interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
  side?: TooltipSide;
  /** 延迟毫秒，默认 300；表格内建议 400（§5.15） */
  delay?: number;
  /** 是否禁用（内容为空/移动端） */
  disabled?: boolean;
  className?: string;
  /** 触发器包裹元素 class（inline-flex 保证布局不变） */
  triggerClassName?: string;
}

const MAX_WIDTH = 240;
const GAP = 8;

export function Tooltip({
  content,
  children,
  side = "top",
  delay = 300,
  disabled = false,
  className,
  triggerClassName,
}: TooltipProps) {
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(null);
  const triggerRef = useRef<HTMLSpanElement>(null);
  const timerRef = useRef<number | null>(null);
  const reduced = useReducedMotion();

  const clearTimer = (): void => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const computePosition = useCallback(() => {
    const node = triggerRef.current;
    if (!node) return;
    const rect = node.getBoundingClientRect();
    const viewportW = window.innerWidth;
    const viewportH = window.innerHeight;
    let top = 0;
    let left = 0;

    if (side === "top" || side === "bottom") {
      top = side === "top" ? rect.top - GAP : rect.bottom + GAP;
      left = rect.left + rect.width / 2;
    } else {
      left = side === "left" ? rect.left - GAP : rect.right + GAP;
      top = rect.top + rect.height / 2;
    }

    // 视口内收，避免出界
    const halfWidth = Math.min(MAX_WIDTH, 200) / 2;
    left = Math.max(halfWidth + 4, Math.min(left, viewportW - halfWidth - 4));
    if (side === "top") top = Math.max(top, 4);
    if (side === "bottom") top = Math.min(top, viewportH - 32);

    setCoords({ top, left });
  }, [side]);

  const show = (): void => {
    if (disabled) return;
    clearTimer();
    timerRef.current = window.setTimeout(() => {
      computePosition();
      setOpen(true);
    }, delay);
  };

  const hide = (): void => {
    clearTimer();
    setOpen(false);
  };

  useEffect(() => clearTimer, []);

  const translate =
    side === "top"
      ? "-50%, -100%"
      : side === "bottom"
        ? "-50%, 0"
        : side === "left"
          ? "-100%, -50%"
          : "0, -50%";

  return (
    <>
      <span
        ref={triggerRef}
        className={cn("inline-flex", triggerClassName)}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        onKeyDown={(event) => {
          if (event.key === "Escape") hide();
        }}
      >
        {children}
      </span>
      {typeof document !== "undefined" &&
        createPortal(
          <AnimatePresence>
            {open && coords && (
              <motion.div
                role="tooltip"
                initial={reduced ? { opacity: 0 } : { opacity: 0, scale: 0.97 }}
                animate={reduced ? { opacity: 1 } : { opacity: 1, scale: 1 }}
                exit={{ opacity: 0, transition: { duration: 0.1, ease: ease.in } }}
                transition={{ duration: 0.12, ease: ease.out }}
                style={{
                  top: coords.top,
                  left: coords.left,
                  transform: `translate(${translate})`,
                  maxWidth: MAX_WIDTH,
                }}
                className={cn(
                  "pointer-events-none fixed z-[80] rounded-sm bg-inverse px-2 py-1",
                  "text-xs leading-4 text-white shadow-2",
                  className
                )}
              >
                {content}
              </motion.div>
            )}
          </AnimatePresence>,
          document.body
        )}
    </>
  );
}
