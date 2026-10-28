// features/warehouse —— 分类栏（全部 / 平台类目 / 自定义分组 / + 分类）
// design-system：chips 用 Badge 视觉 + 计数，高度 28，横向滚动不换行。

import { Folder, Layers, Plus, Tag, X } from "lucide-react";
import { cn, formatCount } from "@/lib/utils";
import type { CategoryFacet, GroupFacet } from "@/lib/types";
import { Button, Skeleton } from "@/components/ui";
import type { WarehouseCategoryFilter } from "@/features/stores";

export interface CategoryBarProps {
  categories: CategoryFacet[];
  groups: GroupFacet[];
  active: WarehouseCategoryFilter;
  total: number;
  loading?: boolean;
  onSelect: (filter: WarehouseCategoryFilter) => void;
  onCreateGroup: () => void;
  onDeleteGroup: (group: GroupFacet) => void;
}

interface ChipProps {
  label: string;
  count: number;
  active: boolean;
  icon?: React.ReactNode;
  onClick: () => void;
  onRemove?: () => void;
}

function Chip({ label, count, active, icon, onClick, onRemove }: ChipProps) {
  return (
    <span
      className={cn(
        "group inline-flex h-7 shrink-0 items-center rounded-md border",
        "transition-colors duration-fast ease-in-out",
        active
          ? "border-brand-600 bg-brand-50 text-brand-700"
          : "border-border bg-surface text-fg-secondary hover:border-border-strong hover:text-fg"
      )}
    >
      <button
        type="button"
        onClick={onClick}
        aria-pressed={active}
        className="inline-flex h-full items-center gap-1.5 rounded-l-md pl-2.5 pr-2 text-[13px] font-medium leading-5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32"
      >
        {icon}
        <span className="max-w-[180px] truncate">{label}</span>
        <span
          className={cn(
            "num rounded-xs px-1 text-2xs leading-4",
            active ? "bg-brand-100 text-brand-800" : "bg-canvas-inset text-fg-muted"
          )}
        >
          {count > 9999 ? "9999+" : formatCount(count)}
        </span>
      </button>
      {onRemove && (
        <button
          type="button"
          aria-label={`删除分组 ${label}`}
          onClick={(event) => {
            event.stopPropagation();
            onRemove();
          }}
          className={cn(
            "grid h-full w-5 place-items-center rounded-r-md text-fg-muted",
            "opacity-0 transition-opacity duration-fast hover:text-danger-600 group-hover:opacity-100"
          )}
        >
          <X className="size-3" strokeWidth={2.4} />
        </button>
      )}
    </span>
  );
}

export function CategoryBar({
  categories,
  groups,
  active,
  total,
  loading = false,
  onSelect,
  onCreateGroup,
  onDeleteGroup,
}: CategoryBarProps) {
  const allActive = active.kind === "all";

  return (
    // min-h 固定住：分类加载态只有 3 个骨架、真实分类十几个，高度来回变会让
    // 下面的列表整体上下跳 —— 这就是「切分类页面会动」的来源。
    <div className="flex min-h-[34px] items-start gap-2">
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
        <Chip
          label="全部"
          count={total}
          active={allActive}
          icon={<Layers className="size-3.5" strokeWidth={2} />}
          onClick={() => onSelect({ kind: "all" })}
        />

        {loading && (
          <>
            {/* 骨架数量与常见分类数对齐，切分类时高度不会突然缩短再撑开 */}
            {["w-24", "w-20", "w-28", "w-24", "w-20", "w-24", "w-28"].map((w, index) => (
              <Skeleton key={`cat-sk-${index}`} className={`h-7 ${w}`} rounded="rounded-md" />
            ))}
          </>
        )}

        {!loading &&
          categories.map((category) => {
            const isActive =
              (active.kind === "cid" && active.cid === category.cid && Boolean(category.cid)) ||
              (active.kind === "categoryId" &&
                Boolean(category.categoryId) &&
                active.categoryId === category.categoryId);
            return (
              <Chip
                key={`cat-${category.cid || category.categoryId || category.name}`}
                label={category.name}
                count={category.count}
                active={isActive}
                icon={<Tag className="size-3.5" strokeWidth={2} />}
                onClick={() => {
                  if (isActive) {
                    onSelect({ kind: "all" });
                    return;
                  }
                  if (category.cid) {
                    onSelect({ kind: "cid", cid: category.cid, name: category.name });
                  } else if (category.categoryId) {
                    onSelect({
                      kind: "categoryId",
                      categoryId: category.categoryId,
                      name: category.name,
                    });
                  }
                }}
              />
            );
          })}

        {!loading &&
          groups.map((group) => {
            const isActive = active.kind === "group" && active.groupId === group.id;
            return (
              <Chip
                key={`grp-${group.id}`}
                label={group.name}
                count={group.count}
                active={isActive}
                icon={<Folder className="size-3.5" strokeWidth={2} />}
                onClick={() =>
                  isActive
                    ? onSelect({ kind: "all" })
                    : onSelect({ kind: "group", groupId: group.id, name: group.name })
                }
                onRemove={() => onDeleteGroup(group)}
              />
            );
          })}
      </div>

      <div className="flex shrink-0 items-center gap-1">
        <Button variant="secondary" size="sm" icon={<Plus />} onClick={onCreateGroup}>
          分类
        </Button>
      </div>
    </div>
  );
}
