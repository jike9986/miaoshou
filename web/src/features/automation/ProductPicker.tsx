// features/automation —— 选品列表（左中区）
//
// 只显示「图片 + 标题」。店铺、站点、入仓状态、序号这些都不要 —— 老版的卡片
// 虽然带meta行，但用户明确要求采集箱这里只留图和名字，看得清最重要。
//
// 列表形态：多列自适应网格，卡片比之前大（图片 56px），标题单行截断。
// 工具栏严格一行：搜索 · 筛选 · 每页条数 · 页码 · 翻页 · 全选 · 清空
// 每页可选 10 / 20 / 50 / 100（后端已放开到 100，向上游翻页凑数）。

import { ScanLine, SearchX, Unplug } from "lucide-react";
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
  SearchInput,
  Select,
  SkeletonRows,
  Switch,
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
  keyword: string;
  autoScan: boolean;
  selectedKeys: ReadonlySet<string>;
  stages: Record<string, ItemStage>;
  /** 每件完成了哪几步；缺省表示还没开始 */
  progress?: Record<string, ItemProgress>;
  onFilterChange: (filter: ListFilter) => void;
  onKeywordChange: (keyword: string) => void;
  onAutoScanChange: (autoScan: boolean) => void;
  onScan: () => void;
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
  keyword,
  autoScan,
  selectedKeys,
  stages,
  progress,
  onFilterChange,
  onKeywordChange,
  onAutoScanChange,
  onScan,
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

  return (
    <section
      className={cn(
        "flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-1",
        className
      )}
      aria-label="选择要处理的商品"
    >
      {/* 标题行：只保留「已选 N」提示，其余交给工具栏 */}
      <div className="flex h-10 shrink-0 items-center justify-between gap-3 border-b border-border-subtle px-3">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[13px] font-medium leading-5 text-fg">选择要处理的商品</span>
          {selectedKeys.size > 0 && (
            <Badge variant="brand" size="sm">
              已选 {selectedKeys.size}
            </Badge>
          )}
        </span>
        <span className="flex shrink-0 items-center gap-2">
          <Switch
            checked={autoScan}
            onChange={onAutoScanChange}
            label="自动扫描"
            disabled={!connected}
          />
          <Button
            size="sm"
            variant="secondary"
            icon={<ScanLine className="size-3.5" />}
            loading={fetching && !loading}
            loadingText="扫描中"
            disabled={!connected || disabled}
            onClick={onScan}
          >
            扫描
          </Button>
        </span>
      </div>

      {/* 单行工具栏 */}
      <div className="flex shrink-0 flex-nowrap items-center gap-1.5 border-b border-border-subtle px-3 py-2">
        <SearchInput
          value={keyword}
          onChange={onKeywordChange}
          placeholder="搜索标题/ID"
          aria-label="搜索商品"
          width="w-[108px]"
          size="sm"
          shortcut={null}
          className="pr-3"
          containerClassName="min-w-[96px] shrink"
        />
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
            title="选中本页全部商品"
            onClick={onSelectVisible}
          >
            全选
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
            title={keyword ? "没有匹配的商品" : "采集箱是空的"}
            description={keyword ? "换个关键词试试。" : "先到妙手后台把商品加入采集箱。"}
            minHeight={220}
          />
        )}

        {connected && !loading && !errorMessage && items.length > 0 && (
          <ul
            className="grid min-w-0 content-start gap-2
                       [grid-template-columns:repeat(auto-fill,minmax(206px,1fr))]"
          >
            {items.map((item) => {
              const stage = stages[item.key];
              const checked = selectedKeys.has(item.key);
              return (
                <li key={item.key}>
                  <label
                    className={cn(
                      // 卡片：勾选框 + 56px 大图 + 标题。只有这两样。
                      "grid min-h-[68px] cursor-pointer grid-cols-[18px_52px_minmax(0,1fr)] items-center gap-2.5",
                      "rounded-md border bg-surface px-2.5 py-2 transition-colors duration-fast",
                      checked
                        ? "border-brand-600/60 bg-brand-50"
                        : cn(
                            "hover:border-border-strong hover:bg-surface-hover",
                            CARD_STATE_CLASS[stage ?? "queued"]
                          ),
                      disabled && "cursor-not-allowed opacity-60"
                    )}
                  >
                    <Checkbox
                      checked={checked}
                      onChange={() => onToggle(item)}
                      disabled={disabled}
                      aria-label={`选择 ${item.title || item.detailId}`}
                    />
                    <span className="size-[52px] shrink-0 overflow-hidden rounded-sm border border-border bg-canvas-inset">
                      <RemoteImage src={item.imageUrl} alt={item.title} />
                    </span>
                    {/* 标题：单行截断，不换行 */}
                    <span
                      className="min-w-0 truncate text-xs font-medium leading-[1.4] text-fg"
                      title={
                        // 卡片上不放文字（会挤掉标题），把「哪一步完成了」放进悬停提示：
                        // 老版用「文字完成 · 图片待处理」表达的就是这个信息。
                        (() => {
                          const done = progress?.[item.key];
                          const base = item.title || "";
                          if (!done) return base;
                          const parts = [
                            done.textDone ? "文字已完成" : null,
                            done.imagesDone ? "图片已完成" : null,
                            done.imageFailures ? `图片 ${done.imageFailures} 张失败` : null,
                          ].filter(Boolean);
                          return parts.length ? `${base}
${parts.join(" · ")}` : base;
                        })()
                      }
                    >
                      {item.title || "（无标题）"}
                    </span>
                  </label>
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
