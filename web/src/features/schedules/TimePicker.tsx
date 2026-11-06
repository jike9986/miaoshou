// features/schedules —— 时间选择器（双列滚轮：时 | 分）。
// 关键修复（2026-11-06）：
//   1. 弹层用 Portal 渲染到 body，外点关闭必须同时检查「触发器」和「弹层面板」
//      两个 ref，否则点弹层会被当成点外部、立刻关掉 → 表现为「点了没反应」。
//   2. 选小时时若当前分钟在该小时下已过期，自动落到该小时第一个可选分钟，
//      不能因为「组合非法」把整次点击吃掉。
//   3. 禁用项用明显的灰底+浅灰字（不加删除线）。

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Clock } from "lucide-react";
import { cn } from "@/lib/utils";

export interface TimePickerProps {
  value: string;                 // HH:MM
  onChange: (value: string) => void;
  invalid?: boolean;
  /** 今天已过的时间点禁用（HH:MM） */
  minTime?: string;
  className?: string;
}

const HOURS = Array.from({ length: 24 }, (_, index) =>
  String(index).padStart(2, "0"));
const MINUTES = Array.from({ length: 60 }, (_, index) =>
  String(index).padStart(2, "0"));
const PANEL_W = 300;
const PANEL_H = 260;
const LIST_H = 200;
const ROW_H = 34;

export function TimePicker({
  value, onChange, invalid = false, minTime, className,
}: TimePickerProps) {
  const [open, setOpen] = useState(false);
  const [panelStyle, setPanelStyle] = useState<React.CSSProperties>({});
  const triggerRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const hourRef = useRef<HTMLDivElement>(null);
  const minuteRef = useRef<HTMLDivElement>(null);

  const [hour, minute] = (value || "00:00").split(":");

  const floor = typeof minTime === "string" && minTime.length > 0 ? minTime : "";

  const disabledHour = (h: string): boolean => {
    if (!floor) return false;
    // 该小时的所有分钟都过期才算整个小时禁用
    return `${h}:59` < floor;
  };

  const disabledMinute = (h: string, m: string): boolean =>
    Boolean(floor) && `${h}:${m}` < floor;

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

  // 打开时把选中项滚到中间
  useEffect(() => {
    if (!open) return;
    const scroll = (list: HTMLElement | null, index: number): void => {
      if (!list || index < 0) return;
      list.scrollTop = Math.max(0, index * ROW_H - LIST_H / 2 + ROW_H / 2);
    };
    scroll(hourRef.current, HOURS.indexOf(hour));
    scroll(minuteRef.current, MINUTES.indexOf(minute));
  }, [open, hour, minute]);

  // 外点关闭：同时检查触发器与弹层面板（Portal 后两者不在同一 DOM 子树）
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

  const selectHour = (h: string): void => {
    if (disabledHour(h)) return;
    // 当前分钟在新小时下过期 → 落到该小时第一个可选分钟
    if (disabledMinute(h, minute)) {
      const firstOk = MINUTES.find((m) => !disabledMinute(h, m));
      onChange(`${h}:${firstOk ?? "00"}`);
      return;
    }
    onChange(`${h}:${minute}`);
  };

  const selectMinute = (m: string): void => {
    if (disabledMinute(hour, m)) return;
    onChange(`${hour}:${m}`);
  };

  const renderColumn = (
    items: string[],
    active: string,
    listRef: React.Ref<HTMLDivElement>,
    onSelect: (item: string) => void,
    isDisabled: (item: string) => boolean,
    label: string,
  ) => (
    <div className="flex-1">
      <p className="mb-1 text-center text-2xs text-fg-muted">{label}</p>
      <div
        ref={listRef}
        className="overflow-y-auto rounded-md border border-border-subtle bg-surface"
        style={{ height: LIST_H }}
      >
        {items.map((item) => {
          const isActive = item === active;
          const off = isDisabled(item);
          return (
            <button
              key={item}
              type="button"
              role="option"
              aria-selected={isActive}
              disabled={off}
              onClick={() => onSelect(item)}
              style={{ height: ROW_H }}
              className={cn(
                "num flex w-full items-center justify-center text-[13px] transition-colors duration-fast",
                isActive
                  ? "bg-brand-600 font-medium text-white"
                  : off
                    ? "cursor-not-allowed text-fg-disabled"
                    : "text-fg hover:bg-brand-50",
              )}
            >
              {item}
            </button>
          );
        })}
      </div>
    </div>
  );

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
        <span className={cn("num", value ? "text-fg" : "text-fg-muted")}>
          {value || "选择时间"}
        </span>
        <Clock className="size-4 shrink-0 text-fg-muted" strokeWidth={1.8} />
      </button>

      {open && createPortal(
        <div
          ref={panelRef}
          role="dialog"
          style={panelStyle}
          className="z-[60] rounded-lg border border-border bg-surface p-2.5 shadow-3"
        >
          <div className="flex gap-2">
            {renderColumn(HOURS, hour, hourRef, selectHour,
              disabledHour, "时")}
            {renderColumn(MINUTES, minute, minuteRef, selectMinute,
              (m) => disabledMinute(hour, m), "分")}
          </div>
          <div className="mt-2 flex items-center justify-between border-t border-border-subtle pt-2">
            <button
              type="button"
              onClick={() => {
                const now = new Date();
                const pad = (v: number): string => String(v).padStart(2, "0");
                const hh = pad(now.getHours());
                const mm = pad(now.getMinutes());
                if (!disabledMinute(hh, mm)) onChange(`${hh}:${mm}`);
              }}
              className="text-[13px] font-medium text-brand-700 hover:underline"
            >
              现在
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-[13px] text-fg-muted hover:text-fg"
            >
              关闭
            </button>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}
