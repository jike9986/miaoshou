// design-system §5.15 其它基础件 —— Tabs / Popover / 键盘按键
// Tabs：高 36，项 px-3 text-sm，未选 text-fg-secondary hover:text-fg，选中 text-fg font-medium
//       + 底部 2px bg-brand-600（layoutId 滑动 200ms ease-out）；分隔线 1px border-subtle。
// Popover：rounded-md border bg-elevated p-3 shadow-2，进入 opacity + scale .97，140ms。

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";
import { ease, popIn } from "@/lib/motion";

/* ------------------------------------------------------------------ *
 * Tabs
 * ------------------------------------------------------------------ */

export interface TabItem<Value extends string = string> {
  value: Value;
  label: ReactNode;
  /** 右侧计数（如「失败 2」） */
  count?: number | null;
  disabled?: boolean;
  icon?: ReactNode;
}

export interface TabsProps<Value extends string = string> {
  value: Value;
  onChange: (value: Value) => void;
  items: TabItem<Value>[];
  className?: string;
  /** 是否显示底部 1px 分隔线 */
  bordered?: boolean;
  /** 右侧附加内容（如「共 1,284 条」） */
  extra?: ReactNode;
  "aria-label"?: string;
}

export function Tabs<Value extends string = string>({
  value,
  onChange,
  items,
  className,
  bordered = true,
  extra,
  "aria-label": ariaLabel = "标签页",
}: TabsProps<Value>) {
  const layoutId = useId();
  const reduced = useReducedMotion();

  return (
    <div
      className={cn(
        "flex items-center gap-1",
        bordered && "border-b border-border-subtle",
        className
      )}
    >
      <div role="tablist" aria-label={ariaLabel} className="flex items-center gap-1">
        {items.map((item) => {
          const active = item.value === value;
          return (
            <button
              key={item.value}
              role="tab"
              type="button"
              aria-selected={active}
              disabled={item.disabled}
              onClick={() => onChange(item.value)}
              className={cn(
                "relative flex h-9 items-center gap-1.5 rounded-sm px-3 text-sm",
                "transition-colors duration-fast ease-in-out",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32",
                active ? "font-medium text-fg" : "text-fg-secondary hover:text-fg",
                item.disabled && "pointer-events-none opacity-50"
              )}
            >
              {item.icon}
              {item.label}
              {typeof item.count === "number" && (
                <span
                  className={cn(
                    "grid h-4 min-w-4 place-items-center rounded-xs px-1 num text-[11px] leading-4",
                    active ? "bg-brand-100 text-brand-800" : "bg-canvas-inset text-fg-muted"
                  )}
                >
                  {item.count > 99 ? "99+" : item.count}
                </span>
              )}
              {active && (
                <motion.span
                  layoutId={reduced ? undefined : `tab-indicator-${layoutId}`}
                  aria-hidden
                  className="absolute inset-x-0 -bottom-px h-0.5 rounded-full bg-brand-600"
                  transition={{ duration: 0.2, ease: ease.out }}
                />
              )}
            </button>
          );
        })}
      </div>
      {extra && <div className="ml-auto shrink-0 text-xs text-fg-muted">{extra}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Popover（点击触发，点击外部/Esc 关闭）
 * ------------------------------------------------------------------ */

export interface PopoverProps {
  trigger: ReactNode;
  children: ReactNode;
  /** 面板对齐方式 */
  align?: "start" | "end" | "center";
  /** 面板宽度类，如 "w-64" */
  panelClassName?: string;
  className?: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

export function Popover({
  trigger,
  children,
  align = "start",
  panelClassName,
  className,
  open: openProp,
  onOpenChange,
}: PopoverProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const open = openProp ?? internalOpen;
  const setOpen = useCallback(
    (next: boolean) => {
      if (openProp === undefined) setInternalOpen(next);
      onOpenChange?.(next);
    },
    [onOpenChange, openProp]
  );

  const rootRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent): void => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, setOpen]);

  return (
    <div ref={rootRef} className={cn("relative inline-flex", className)}>
      <div
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setOpen(!open);
          }
        }}
        aria-expanded={open}
        aria-haspopup="dialog"
        role="button"
        tabIndex={0}
        className="inline-flex focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32 rounded-sm"
      >
        {trigger}
      </div>
      <AnimatePresence>
        {open && (
          <motion.div
            ref={panelRef}
            role="dialog"
            variants={
              reduced
                ? { hidden: { opacity: 0 }, show: { opacity: 1 }, exit: { opacity: 0 } }
                : popIn
            }
            initial="hidden"
            animate="show"
            exit="exit"
            className={cn(
              "absolute top-[calc(100%+6px)] z-40 min-w-[180px] rounded-md border border-border bg-elevated p-3 shadow-2",
              align === "end" ? "right-0" : align === "center" ? "left-1/2 -translate-x-1/2" : "left-0",
              panelClassName
            )}
          >
            {children}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 反色浮层（Tooltip 的静态版本；批量操作栏 / 快捷键提示可复用）
 * ------------------------------------------------------------------ */

export function InverseSurface({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cn("rounded-sm bg-inverse px-2 py-1 text-xs leading-4 text-white shadow-2", className)}>
      {children}
    </span>
  );
}

/** 仅在浏览器环境渲染的子节点（Portal 用；SSR 安全） */
export function ClientOnly({ children }: { children: ReactNode }) {
  if (typeof document === "undefined") return null;
  return createPortal(children, document.body);
}
