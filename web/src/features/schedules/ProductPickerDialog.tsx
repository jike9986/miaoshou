// features/schedules —— 定时任务的仓库商品选择弹窗。
// 图片网格 + 认领状态筛选（负责人 2026-11-05 要求）：
//   · 每个商品显示首图（cover），不放大段标题文字；
//   · 顶部筛选：全部 / 已认领 / 未认领；
//   · 点确认把选中的 id 交回给表单。

import { useMemo, useState } from "react";
import { Dialog, Button, SkeletonRows } from "@/components/ui";
import { cn } from "@/lib/utils";

export interface PickerProduct {
  id: string;
  title: string;
  images: string[];
  status: string;
  claimed: boolean;
}

export interface ProductPickerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  products: PickerProduct[];
  loading: boolean;
  selected: string[];
  onConfirm: (ids: string[]) => void;
  hasMore?: boolean;
  page: number;
  onPageChange: (page: number) => void;
  totalPages: number;
}

type Filter = "all" | "claimed" | "unclaimed";

const FILTER_OPTIONS: Array<{ value: Filter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "claimed", label: "已认领" },
  { value: "unclaimed", label: "未认领" },
];

export function ProductPickerDialog({
  open, onOpenChange, products, loading, selected, onConfirm,
  hasMore, page, onPageChange, totalPages,
}: ProductPickerDialogProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const [pending, setPending] = useState<string[]>(selected);

  // 打开时同步外层已选
  const key = products.map((p) => p.id).join(",") + ":" + selected.join(",");
  useMemo(() => {
    if (open) setPending(selected);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, key]);

  const filtered = useMemo(() => {
    if (filter === "claimed") return products.filter((p) => p.claimed);
    if (filter === "unclaimed") return products.filter((p) => !p.claimed);
    return products;
  }, [products, filter]);

  const toggle = (id: string): void => {
    setPending((current) =>
      current.includes(id) ? current.filter((x) => x !== id) : [...current, id]);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setPending(selected);
        onOpenChange(next);
      }}
      size="lg"
      title="选择发布商品"
      description={`已选 ${pending.length} 件 · 点击图片勾选/取消`}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>取消</Button>
          <Button variant="primary" onClick={() => onConfirm(pending)}>
            确定（{pending.length} 件）
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <div className="flex gap-2">
            {FILTER_OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                aria-pressed={filter === option.value}
                onClick={() => setFilter(option.value)}
                className={cn(
                  "h-8 rounded-md border px-3 text-[13px] font-medium transition-colors duration-fast",
                  filter === option.value
                    ? "border-brand-600 bg-brand-50 text-brand-700"
                    : "border-border bg-surface text-fg-secondary hover:border-border-strong",
                )}
              >
                {option.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => {
              const pageIds = filtered.map((p) => p.id);
              const allSelected = pageIds.length > 0
                && pageIds.every((id) => pending.includes(id));
              setPending((current) => {
                if (allSelected) {
                  return current.filter((id) => !pageIds.includes(id));
                }
                return [...new Set([...current, ...pageIds])];
              });
            }}
            className="h-8 rounded-md border border-border px-3 text-[13px] font-medium text-fg-secondary transition-colors duration-fast hover:border-border-strong"
          >
            {(() => {
              const pageIds = filtered.map((p) => p.id);
              const allSelected = pageIds.length > 0
                && pageIds.every((id) => pending.includes(id));
              return allSelected ? "取消全选本页" : "全选本页";
            })()}
          </button>
        </div>

        <div className="max-h-[46vh] overflow-y-auto rounded-md border border-border-subtle p-2">
          {loading ? (
            <SkeletonRows rows={4} />
          ) : filtered.length === 0 ? (
            <p className="p-6 text-center text-sm text-fg-muted">
              {filter === "claimed" ? "没有已认领的商品" :
                filter === "unclaimed" ? "没有未认领的商品" : "当前账号没有可选仓库商品"}
            </p>
          ) : (
            <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">
              {filtered.map((product) => {
                const cover = product.images[0] ?? "";
                const checked = pending.includes(product.id);
                return (
                  <button
                    key={product.id}
                    type="button"
                    onClick={() => toggle(product.id)}
                    className={cn(
                      "group relative overflow-hidden rounded-md border-2 transition-colors duration-fast",
                      checked ? "border-brand-600" : "border-transparent hover:border-border-strong",
                    )}
                    title={product.title}
                  >
                    <div className="aspect-square w-full bg-surface-hover">
                      {cover ? (
                        <img
                          src={cover}
                          alt=""
                          loading="lazy"
                          className="size-full object-cover"
                        />
                      ) : (
                        <div className="grid size-full place-items-center text-2xs text-fg-muted">
                          无图
                        </div>
                      )}
                    </div>
                    <span
                      className={cn(
                        "absolute left-1 top-1 grid size-4 place-items-center rounded-[3px] border transition-colors",
                        checked
                          ? "border-brand-600 bg-brand-600 text-white"
                          : "border-white/70 bg-white/85",
                      )}
                      aria-hidden
                    >
                      {checked && (
                        <svg viewBox="0 0 12 12" className="size-3" fill="none">
                          <path d="M2 6.2 4.6 8.8 10 3.4" stroke="currentColor"
                            strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      )}
                    </span>
                    <span
                      className={cn(
                        "absolute bottom-0 left-0 right-0 truncate px-1 py-0.5 text-2xs font-medium",
                        product.claimed
                          ? "bg-success-600/85 text-white"
                          : "bg-white/85 text-fg-muted",
                      )}
                    >
                      {product.claimed ? "已认领" : "未认领"}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between">
          <Button size="sm" variant="ghost" disabled={page <= 1}
            onClick={() => onPageChange(page - 1)}>上一页</Button>
          <span className="text-xs text-fg-muted">第 {page} 页 / 共 {Math.max(1, totalPages)} 页</span>
          <Button size="sm" variant="ghost" disabled={!hasMore}
            onClick={() => onPageChange(page + 1)}>下一页</Button>
        </div>
      </div>
    </Dialog>
  );
}
