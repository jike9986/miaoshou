// features/warehouse —— 分类栏（全部 / 平台类目 / 自定义分组 / + 分类）
// design-system：chips 用 Badge 视觉 + 计数，高度 28，横向滚动不换行。

import { useRef, useState } from "react";
import { Folder, Layers, Plus, Trash2, X } from "lucide-react";
import { cn, formatCount } from "@/lib/utils";
import type { CategoryFacet, GroupFacet } from "@/lib/types";
import { Button, Skeleton } from "@/components/ui";
import type { WarehouseCategoryFilter } from "@/features/stores";

export interface CategoryBarProps {
  /** 已废弃：平台类目不再显示在分类栏（只显示自定义分组）。
   *  保留字段避免破坏调用方。 */
  categories?: CategoryFacet[];
  groups: GroupFacet[];
  active: WarehouseCategoryFilter;
  total: number;
  loading?: boolean;
  onSelect: (filter: WarehouseCategoryFilter) => void;
  onCreateGroup: () => void;
  onDeleteGroup: (group: GroupFacet) => void;
  /** 拖拽排序后回调（老师/管理员才有）。传完整的新顺序 id 列表。 */
  onReorder?: (orderedIds: string[]) => void;
  /**
   * 是否可管理分类（新建 / 删除分组）。
   * 学员只有 `warehouse` 权限，后端 `/groups` 写接口要 `process` 权限，
   * 所以学员看得到、点得动分类筛选，但不显示「+ 分类」和删除按钮。
   */
  manageable?: boolean;
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
          // 删除模式下 × 必须常驻可见：原来是 opacity-0 + hover 才显，
          // 运营进了删除模式却看不到能点哪里（2026-11-12 负责人要求
          // 「点击删除再点击分类右边的删除」）。
          className={cn(
            "grid h-full w-5 place-items-center rounded-r-md",
            "bg-danger-50 text-danger-600 transition-colors duration-fast hover:bg-danger-100"
          )}
        >
          <X className="size-3" strokeWidth={2.4} />
        </button>
      )}
    </span>
  );
}

export function CategoryBar({
  groups,
  active,
  total,
  loading = false,
  onSelect,
  onCreateGroup,
  onDeleteGroup,
  onReorder,
  manageable = true,
}: CategoryBarProps) {
  const allActive = active.kind === "all";
  // 删除模式：默认不显示每个分组右侧的 ×，点「删除」按钮才进入。
  // 负责人口径 2026-11-12：分组右边的 × 平时太容易误点（一点就删组），
  // 必须先点「删除」进入删除态，再点具体分组上的 × 才真删。
  const [deleteMode, setDeleteMode] = useState(false);
  // 拖拽排序（负责人口径 2026-11-12：分类可以长按拖动顺序，只能老师）。
  // 用 HTML5 draggable 而不是引入 dnd 库：这里就一排 chip，原生够用，
  // 也不会给主包再加一个依赖。
  const dragIndexRef = useRef<number | null>(null);
  const [draggingId, setDraggingId] = useState<string | null>(null);

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

        {/* 平台类目不再出现在分类栏里（负责人口径 2026-11-12：
            「现在这六个全部是自定义分组」——分类栏只放仓库自己的分组，
            平台类目（运动鞋 / 跑步鞋 / 轮滑鞋…）属于商品属性，
            只在卡片右下角显示。分类栏混进平台类目会让人以为它们也是分组。
            平台类目仍可通过商品卡片右下角查看，筛选走搜索。 */}
        {!loading &&
          groups.map((group, index) => {
            const isActive = active.kind === "group" && active.groupId === group.id;
            const canDrag = manageable && Boolean(onReorder) && !deleteMode;
            return (
              <span
                key={`grp-${group.id}`}
                draggable={canDrag}
                onDragStart={(event) => {
                  if (!canDrag) return;
                  dragIndexRef.current = index;
                  setDraggingId(group.id);
                  event.dataTransfer.effectAllowed = "move";
                  // Firefox 必须 setData 才会真正开始拖拽
                  event.dataTransfer.setData("text/plain", group.id);
                }}
                onDragOver={(event) => {
                  if (!canDrag || dragIndexRef.current === null) return;
                  event.preventDefault();
                  event.dataTransfer.dropEffect = "move";
                }}
                onDrop={(event) => {
                  if (!canDrag) return;
                  event.preventDefault();
                  const from = dragIndexRef.current;
                  dragIndexRef.current = null;
                  setDraggingId(null);
                  if (from === null || from === index) return;
                  const next = groups.map((item) => String(item.id));
                  const [moved] = next.splice(from, 1);
                  next.splice(index, 0, moved);
                  onReorder?.(next);
                }}
                onDragEnd={() => {
                  dragIndexRef.current = null;
                  setDraggingId(null);
                }}
                className={cn(
                  "inline-flex",
                  canDrag && "cursor-grab active:cursor-grabbing",
                  draggingId === group.id && "opacity-40"
                )}
                title={canDrag ? "按住拖动可以调整顺序" : undefined}
              >
                <Chip
                  label={group.name}
                  count={group.count}
                  active={isActive}
                  icon={<Folder className="size-3.5" strokeWidth={2} />}
                  onClick={() =>
                    isActive
                      ? onSelect({ kind: "all" })
                      : onSelect({ kind: "group", groupId: group.id, name: group.name })
                  }
                  onRemove={
                    manageable && deleteMode
                      ? () => {
                          onDeleteGroup(group);
                          setDeleteMode(false);
                        }
                      : undefined
                  }
                />
              </span>
            );
          })}
      </div>

      {manageable && (
        <div className="flex shrink-0 items-center gap-1">
          <Button
            variant={deleteMode ? "primary" : "secondary"}
            size="sm"
            icon={deleteMode ? <X /> : <Trash2 />}
            onClick={() => setDeleteMode((value) => !value)}
            title={deleteMode ? "退出删除模式" : "进入删除模式后，点分组上的 × 删除该分组"}
          >
            {deleteMode ? "取消" : "删除"}
          </Button>
          <Button variant="secondary" size="sm" icon={<Plus />} onClick={onCreateGroup}>
            分类
          </Button>
        </div>
      )}
    </div>
  );
}
