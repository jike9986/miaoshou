// features/schedules —— 日期选择器（输入框 + 月历弹窗）。
// 关键修复（2026-11-06）：
//   1. 弹层 Portal 到 body 后，外点关闭必须同时检查「触发器」和「弹层面板」两个 ref，
//      否则点弹层会被当成点外部、立刻关闭 → 表现为「点了没反应」。
//   2. 禁用日期用明显灰底+浅灰字（不加删除线）。

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

export interface DatePickerProps {
  value: string;                 // YYYY-MM-DD，空串表示未选
  onChange: (value: string) => void;
  placeholder?: string;
  invalid?: boolean;
  /** 最早可选日期（YYYY-MM-DD） */
  minDate?: string;
  className?: string;
}

const WEEK_LABELS = ["一", "二", "三", "四", "五", "六", "日"];
const PANEL_W = 300;
const PANEL_H = 360;
const CELL_H = 36;

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function toIso(year: number, month: number, day: number): string {
  return `${year}-${pad(month + 1)}-${pad(day)}`;
}

function todayIso(): string {
  const now = new Date();
  return toIso(now.getFullYear(), now.getMonth(), now.getDate());
}

export function DatePicker({
  value, onChange, placeholder = "选择日期", invalid = false, minDate, className,
}: DatePickerProps) {
  const [open, setOpen] = useState(false);
  const [panelStyle, setPanelStyle] = useState<React.CSSProperties>({});
  const triggerRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const initial = value ? new Date(`${value}T00:00:00`) : new Date();
  const [viewYear, setViewYear] = useState(initial.getFullYear());
  const [viewMonth, setViewMonth] = useState(initial.getMonth());

  const floor = minDate ?? "";
  const today = todayIso();

  // 定位：Portal + fixed，空间不足向上翻
  useEffect(() => {
    if (!open || !triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    const flip = window.innerHeight - rect.bottom < PANEL_H + 12
      && rect.top > PANEL_H + 12;
    setPanelStyle({
      position: "fixed",
      left: Math.max(8, Math.min(rect.left, window.innerWidth - PANEL_W - 8)),
      top: flip ? Math.max(8, rect.top - PANEL_H - 6) : rect.bottom + 6,
      width: PANEL_W,
    });
  }, [open]);

  // 打开时视图跳到已选月份
  useEffect(() => {
    if (!open) return;
    const target = value ? new Date(`${value}T00:00:00`) : new Date();
    setViewYear(target.getFullYear());
    setViewMonth(target.getMonth());
  }, [open, value]);

  // 外点关闭：同时检查触发器与弹层面板
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent): void => {
      const target = event.target as Node;
      const inTrigger = triggerRef.current?.contains(target);
      const inPanel = panelRef.current?.contains(target);
      if (!inTrigger && !inPanel) setOpen(false);
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
  }, [open]);

  const cells = useMemo(() => {
    const first = new Date(viewYear, viewMonth, 1);
    const leading = (first.getDay() + 6) % 7;   // 周一为第一列
    const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
    const list: Array<{ iso: string; day: number } | null> = [];
    for (let i = 0; i < leading; i += 1) list.push(null);
    for (let day = 1; day <= daysInMonth; day += 1) {
      list.push({ iso: toIso(viewYear, viewMonth, day), day });
    }
    while (list.length % 7 !== 0) list.push(null);
    return list;
  }, [viewYear, viewMonth]);

  const isDisabled = (iso: string): boolean => Boolean(floor) && iso < floor;

  const shiftMonth = (delta: number): void => {
    const next = new Date(viewYear, viewMonth + delta, 1);
    setViewYear(next.getFullYear());
    setViewMonth(next.getMonth());
  };

  return (
    <div ref={triggerRef} className={cn("relative", className)}>
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-haspopup="dialog"
        aria-expanded={open}
        className={cn(
          "flex h-9 w-full items-center justify-between gap-2 rounded-md border bg-surface px-3 text-left text-[13px]",
          "transition-colors duration-fast ease-in-out",
          invalid
            ? "border-danger-600 text-danger-700"
            : "border-border text-fg hover:border-border-strong",
          open && !invalid && "border-brand-600",
        )}
      >
        <span className={value ? "num text-fg" : "text-fg-muted"}>
          {value || placeholder}
        </span>
        <CalendarDays className="size-4 shrink-0 text-fg-muted" strokeWidth={1.8} />
      </button>

      {open && createPortal(
        <div
          ref={panelRef}
          role="dialog"
          style={panelStyle}
          className="z-[60] rounded-lg border border-border bg-surface p-3 shadow-3"
        >
          <div className="mb-2 flex items-center justify-between">
            <button
              type="button"
              aria-label="上个月"
              onClick={() => shiftMonth(-1)}
              className="grid size-8 place-items-center rounded-sm text-fg-secondary transition-colors hover:bg-surface-hover"
            >
              <ChevronLeft className="size-4" />
            </button>
            <span className="num text-[14px] font-medium">
              {viewYear} 年 {viewMonth + 1} 月
            </span>
            <button
              type="button"
              aria-label="下个月"
              onClick={() => shiftMonth(1)}
              className="grid size-8 place-items-center rounded-sm text-fg-secondary transition-colors hover:bg-surface-hover"
            >
              <ChevronRight className="size-4" />
            </button>
          </div>

          <div className="mb-1 grid grid-cols-7">
            {WEEK_LABELS.map((label) => (
              <span key={label}
                className="grid h-8 place-items-center text-2xs text-fg-muted">
                {label}
              </span>
            ))}
          </div>

          <div className="grid grid-cols-7 gap-1">
            {cells.map((cell, index) => {
              if (!cell) return <span key={`blank-${index}`} style={{ height: CELL_H }} />;
              const selected = value === cell.iso;
              const isToday = cell.iso === today;
              const off = isDisabled(cell.iso);
              return (
                <button
                  key={cell.iso}
                  type="button"
                  disabled={off}
                  aria-pressed={selected}
                  onClick={() => {
                    onChange(cell.iso);
                    setOpen(false);
                  }}
                  style={{ height: CELL_H }}
                  className={cn(
                    "num grid place-items-center rounded-md text-[13px] transition-colors duration-fast",
                    selected
                      ? "bg-brand-600 font-medium text-white"
                      : off
                        ? "cursor-not-allowed text-fg-disabled"
                        : "text-fg hover:bg-brand-50",
                    !selected && !off && isToday && "ring-1 ring-inset ring-brand-500",
                  )}
                >
                  {cell.day}
                </button>
              );
            })}
          </div>

          <div className="mt-2 flex items-center justify-between border-t border-border-subtle pt-2">
            <button
              type="button"
              onClick={() => {
                onChange(today);
                setOpen(false);
              }}
              className="text-[13px] font-medium text-brand-700 hover:underline"
            >
              今天
            </button>
            <button
              type="button"
              onClick={() => {
                onChange("");
                setOpen(false);
              }}
              className="text-[13px] text-fg-muted hover:text-fg"
            >
              清除
            </button>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}
