// 业务页公共：防抖值、分页控件、店铺选择、错误文案。
// 组件一律复用设计系统，不新造按钮/输入框样式。

import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { formatCount } from "@/lib/utils";
import { Button, IconButton, Select } from "@/components/ui";

/* ------------------------------------------------------------------ *
 * 防抖值（搜索框 300ms，api.md §4.1 q）
 * ------------------------------------------------------------------ */

export function useDebouncedValue<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

/* ------------------------------------------------------------------ *
 * 分页：上一页 / 下一页 + 页码信息 + 每页条数（design-system §5.8）
 * ------------------------------------------------------------------ */

export interface PaginationOption {
  value: number;
  label: string;
}

export interface PaginationProps {
  page: number;
  pageSize: number;
  total: number;
  hasMore?: boolean;
  /** 每页条数候选；不传则不显示下拉 */
  pageSizeOptions?: PaginationOption[];
  onPageChange: (page: number) => void;
  onPageSizeChange?: (pageSize: number) => void;
  disabled?: boolean;
  /** 右上角附加信息（如「已扫描 12:04」） */
  extra?: React.ReactNode;
  /** 是否显示「共 N 条」文字（默认显示；列表页头已有总数时关掉） */
  showTotal?: boolean;
  /** 是否显示「第 X / Y 页」（默认显示；老版没有这段数字） */
  showPageNumber?: boolean;
  className?: string;
}

export function Pagination({
  page,
  pageSize,
  total,
  hasMore,
  pageSizeOptions,
  onPageChange,
  onPageSizeChange,
  disabled = false,
  extra,
  showTotal = true,
  showPageNumber = true,
  className,
}: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / Math.max(1, pageSize)));
  const canPrev = page > 1 && !disabled;
  const canNext = (hasMore ?? page < totalPages) && !disabled;

  return (
    <div className={cn("flex flex-wrap items-center justify-between gap-3", className)}>
      <div className="flex shrink-0 items-center gap-3 whitespace-nowrap text-xs text-fg-muted">
        {showTotal && <span className="num">共 {formatCount(total)} 条</span>}
        {extra}
      </div>
      <div className="flex shrink-0 items-center gap-2 whitespace-nowrap">
        {pageSizeOptions && pageSizeOptions.length > 0 && onPageSizeChange && (
          <Select
            size="sm"
            aria-label="每页条数"
            className="w-[104px] shrink-0"
            value={String(pageSize)}
            onChange={(value) => onPageSizeChange(Number(value))}
            options={pageSizeOptions.map((option) => ({
              value: String(option.value),
              label: option.label,
            }))}
          />
        )}
        {showPageNumber && (
          <span className="num shrink-0 text-xs text-fg-muted">
            第 {page} / {totalPages} 页
          </span>
        )}
        <IconButton
          label="上一页"
          size="sm"
          variant="secondary"
          icon={<ChevronLeft />}
          disabled={!canPrev}
          onClick={() => onPageChange(Math.max(1, page - 1))}
        />
        <IconButton
          label="下一页"
          size="sm"
          variant="secondary"
          icon={<ChevronRight />}
          disabled={!canNext}
          onClick={() => onPageChange(page + 1)}
        />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 空状态里的次要动作按钮（文字按钮，§5.14）
 * ------------------------------------------------------------------ */

export function TextAction({
  children,
  onClick,
  className,
}: {
  children: React.ReactNode;
  onClick: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-sm px-2 py-1 text-[13px] font-medium text-brand-700 transition-colors duration-fast",
        "hover:bg-brand-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32",
        className
      )}
    >
      {children}
    </button>
  );
}

/** 「未连接妙手」时的统一提示条动作 */
export function ConnectHintBar({
  onGoUnlock,
  message = "尚未连接妙手账号：采集箱、发布与定时任务需要连接后才能使用。",
  className,
}: {
  onGoUnlock: () => void;
  message?: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex items-center gap-3 rounded-md border border-warning-600/20 bg-warning-50 px-3 py-2.5",
        "text-[13px] leading-5 text-warning-700",
        className
      )}
    >
      <span className="min-w-0 flex-1">{message}</span>
      <Button size="sm" variant="secondary" onClick={onGoUnlock}>
        去连接
      </Button>
    </div>
  );
}
