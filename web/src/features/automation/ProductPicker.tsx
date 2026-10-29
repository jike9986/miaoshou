// features/automation —— 选品列表（左中区）
//
// 只显示「图片 + 标题」。店铺、站点、入仓状态、序号这些都不要 —— 老版的卡片
// 虽然带meta行，但用户明确要求采集箱这里只留图和名字，看得清最重要。
//
// 列表形态：多列自适应网格，卡片比之前大（图片 56px），标题单行截断。
// 工具栏严格一行：搜索 · 筛选 · 每页条数 · 页码 · 翻页 · 全选 · 清空
// 每页可选 10 / 20 / 50 / 100（后端已放开到 100，向上游翻页凑数）。

import { RefreshCw, SearchX, Unplug } from "lucide-react";
import { cn } from "@/lib/utils";
import type { CollectBoxItem } from "@/lib/types";
import { RemoteImage } from "@/features/shared/RemoteImage";
import { Pagination } from "@/features/shared";
import {
  Badge,
  Button,
  Checkbox,
  EmptyState,
  ErrorState,
  Select,
  SkeletonRows,
} from "@/components/ui";
import {
  LIST_FILTER_OPTIONS,
  type ItemStage,
  type ItemProgress,
  type ListFilter,
} from "@/features/automation/types";

/** 后端 COLLECT_BOX_PAGE_MAX = 100，所以给到 100 */
const PAGE_SIZE_OPTIONS = [
  { value: 10, label: "10" },
  { value: 20, label: "20" },
  { value: 50, label: "50" },
  { value: 100, label: "100" },
];

/** 卡片状态色 */
const CARD_STATE_CLASS: Record<ItemStage, string> = {
  queued: "border-border",
  running: "border-info-600/50 bg-info-50/40",
  done: "border-success-600/40 bg-success-50/40",
  failed: "border-danger-600/40 bg-danger-50/40",
};

export interface ProductPickerProps {
  connected: boolean;
  items: CollectBoxItem[];
  total: number;
  hasMore: boolean;
  page: number;
  pageSize: number;
  loading: boolean;
  fetching: boolean;
  error: unknown;
  visibleCount: number;
  filter: ListFilter;
  selectedKeys: ReadonlySet<string>;
  stages: Record<string, ItemStage>;
  /** 每件完成了哪几步；缺省表示还没开始 */
  progress?: Record<string, ItemProgress>;
  onFilterChange: (filter: ListFilter) => void;
  onRefresh: () => void;
  onRetry: () => void;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  onToggle: (item: CollectBoxItem) => void;
  onSelectVisible: () => void;
  onClearSelection: () => void;
  disabled?: boolean;
  className?: string;
}

export function ProductPicker({
  connected,
  items,
  total,
  hasMore,
  page,
  pageSize,
  loading,
  fetching,
  error,
  filter,
  selectedKeys,
  stages,
  progress,
  onFilterChange,
  onRefresh,
  onRetry,
  onPageChange,
  onPageSizeChange,
  onToggle,
  onSelectVisible,
  onClearSelection,
  disabled = false,
  className,
}: ProductPickerProps) {
  const errorMessage =
    error instanceof Error ? error.message : typeof error === "string" ? error : null;
  const allVisibleSelected = items.length > 0 && items.every((item) => selectedKeys.has(item.key));

  return (
    <section
      className={cn(
        "flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-1",
        className
      )}
      aria-label="采集箱"
    >
      {/* 标题行：只保留「已选 N」提示，其余交给工具栏 */}
      <div className="flex h-10 shrink-0 items-center justify-between gap-3 border-b border-border-subtle px-3">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[13px] font-medium leading-5 text-fg">采集箱</span>
          {selectedKeys.size > 0 && (
            <Badge variant="brand" size="sm">
              已选 {selectedKeys.size}
            </Badge>
          )}
        </span>
        <Button
          size="sm"
          variant="secondary"
          icon={<RefreshCw className="size-3.5" />}
          loading={fetching && !loading}
          loadingText="刷新中"
          disabled={!connected || disabled}
          onClick={onRefresh}
        >
          刷新
        </Button>
      </div>

      {/* 单行工具栏：筛选 + 紧凑分页 + 本页全选 */}
      <div className="flex shrink-0 flex-nowrap items-center gap-1.5 border-b border-border-subtle px-3 py-2">
        <Select
          size="sm"
          aria-label="筛选"
          className="w-[80px] shrink-0"
          value={filter}
          onChange={(value) => onFilterChange(value as ListFilter)}
          options={LIST_FILTER_OPTIONS.map((option) => ({
            value: option.value,
            label: option.label,
          }))}
        />

        <div className="ml-auto flex min-w-0 items-center gap-1.5">
          <Pagination
            page={page}
            pageSize={pageSize}
            total={total}
            hasMore={hasMore}
            pageSizeOptions={PAGE_SIZE_OPTIONS}
            onPageChange={onPageChange}
            onPageSizeChange={onPageSizeChange}
            disabled={disabled || !connected}
            showTotal={false}
            showPageNumber={false}
            className="min-w-0 flex-nowrap justify-end gap-2"
            extra={null}
          />
          <Button
            size="sm"
            variant="secondary"
            className="shrink-0"
            disabled={!connected || items.length === 0 || disabled}
            title={allVisibleSelected ? "取消本页全部选中" : "选中本页全部商品"}
            onClick={onSelectVisible}
          >
            {allVisibleSelected ? "取消全选" : "全选"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="shrink-0"
            disabled={selectedKeys.size === 0 || disabled}
            onClick={onClearSelection}
          >
            清空
          </Button>
        </div>
      </div>

      {/* 列表体：只有图片 + 标题 */}
      <div className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden p-2">
        {!connected && (
          <EmptyState
            icon={<Unplug strokeWidth={1.5} />}
            title="先连接妙手"
            description="到左侧「连接妙手」填写 APP ID / App Secret。"
            minHeight={220}
          />
        )}

        {connected && loading && (
          <div className="p-1">
            <SkeletonRows rows={6} />
          </div>
        )}

        {connected && !loading && errorMessage && (
          <ErrorState
            title="采集箱读取失败"
            description={errorMessage}
            onRetry={onRetry}
            retrying={fetching}
            minHeight={220}
          />
        )}

        {connected && !loading && !errorMessage && items.length === 0 && (
          <EmptyState
            icon={<SearchX strokeWidth={1.5} />}
            title="采集箱是空的"
            description="先到妙手后台把商品加入采集箱。"
            minHeight={220}
          />
        )}

        {connected && !loading && !errorMessage && items.length > 0 && (
          <ul
            className="grid min-w-0 content-start gap-2.5
                       [grid-template-columns:repeat(auto-fill,minmax(128px,1fr))]"
          >
            {items.map((item) => {
              const stage = stages[item.key];
              const checked = selectedKeys.has(item.key);
              return (
                <li key={item.key}>
                  {/* 竖版卡片：方图在上、两行标题在下，勾选框与仓库统一浮在图右上角。
                      一行 3 个的横版在宽屏下把标题挤得只剩几个字，竖版让图成为主角。 */}
                  <div
                    className={cn(
                      "group relative flex cursor-pointer flex-col overflow-hidden rounded-md border bg-surface",
                      "transition-[border-color,box-shadow] duration-fast",
                      checked
                        ? "border-brand-600 ring-2 ring-brand-600/24"
                        : cn(
                            "hover:border-border-strong hover:shadow-1",
                            CARD_STATE_CLASS[stage ?? "queued"]
                          ),
                      disabled && "cursor-not-allowed opacity-60"
                    )}
                  >
                    <label className="relative block cursor-pointer">
                      <span className="absolute right-2 top-2 z-[1]">
                        <Checkbox
                          checked={checked}
                          onChange={() => onToggle(item)}
                          disabled={disabled}
                          aria-label={`选择 ${item.title || item.detailId}`}
                        />
                      </span>
                      <span className="block aspect-square w-full overflow-hidden bg-canvas-inset">
                        <RemoteImage src={item.imageUrl} alt={item.title} />
                      </span>
                    </label>
                    <button
                      type="button"
                      onClick={() => onToggle(item)}
                      disabled={disabled}
                      title={(() => {
                        const done = progress?.[item.key];
                        const base = item.title || "";
                        if (!done) return base;
                        const parts = [
                          done.textDone ? "文字已完成" : null,
                          done.imagesDone ? "图片已完成" : null,
                          done.imageFailures ? `图片 ${done.imageFailures} 张失败` : null,
                        ].filter(Boolean);
                        return parts.length ? `${base}\n${parts.join(" · ")}` : base;
                      })()}
                      className={cn(
                        "block w-full px-2 pb-1.5 pt-1 text-left",
                        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-600/32"
                      )}
                    >
                      {/* 两行标题，超出省略 —— 比单行多露一倍信息，又不至于撑高卡片 */}
                      <span className="line-clamp-2 min-h-[32px] text-xs font-medium leading-4 text-fg">
                        {item.title || "（无标题）"}
                      </span>
                    </button>
                    {stage && stage !== "queued" && (
                      <span
                        className={cn(
                          "absolute left-1.5 top-1.5 rounded px-1 py-0.5 text-[10px] font-medium leading-3",
                          stage === "running" && "bg-info-600 text-white",
                          stage === "done" && "bg-success-600 text-white",
                          stage === "failed" && "bg-danger-600 text-white"
                        )}
                      >
                        {stage === "running" ? "处理中" : stage === "done" ? "完成" : "失败"}
                      </span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}

export default ProductPicker;
