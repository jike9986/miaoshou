// 三栏联动类目选择器（对齐妙手后台的类目选择体验）
//
// 为什么不用下拉框：
//   妙手类目是三层结构（父类目 → 子类目 → 叶子），叶子有 2000+ 个。
//   一次性摊进下拉里靠搜索找人，既看不出层级也不知道选到了哪一支。
//   三栏联动把「逐层下钻」直接映射成三列，左边点了看中间，中间点了看右边，
//   最右列点一下才是真正选中 —— 和妙手后台操作完全一致。
//
// 三个细节：
//   · 被平台标记 disabled 的类目灰掉且不可点（选了也会被上游拒）；
//   · 每列独立上下滚动，三列高度一致，不会互相撑；
//   · 顶部「历史记录」是最近选过的叶子，跨商品复用，存在 localStorage。
import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, Clock, Search, Tag } from "lucide-react";
import { Badge, Input } from "@/components/ui";
import { cn } from "@/lib/utils";

/** 与 CategoryNode（lib/types）保持同构；这里独立声明避免循环依赖。 */
export interface PickerNode {
  cid: string;
  name: string;
  nameZh?: string;
  path: string;
  isLeaf: boolean;
  disabled?: boolean;
  children?: PickerNode[];
}

export interface CategoryPickerProps {
  tree: PickerNode[];
  /** 当前选中的叶子 cid */
  value: string | null;
  onChange: (cid: string | null) => void;
  /** 最多保留几条历史 */
  historyLimit?: number;
  className?: string;
}

const HISTORY_KEY = "aohuang.category-history.v1";
const DEFAULT_HISTORY_LIMIT = 6;

interface HistoryEntry {
  cid: string;
  label: string;
}

function readHistory(): HistoryEntry[] {
  try {
    const raw = window.localStorage.getItem(HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is HistoryEntry =>
        typeof item?.cid === "string" && typeof item?.label === "string")
      .slice(0, DEFAULT_HISTORY_LIMIT);
  } catch {
    return [];
  }
}

function writeHistory(entries: HistoryEntry[]): void {
  try {
    window.localStorage.setItem(HISTORY_KEY, JSON.stringify(entries));
  } catch {
    // 存储满或被禁用时忽略：历史只是便利功能
  }
}

function labelOf(node: PickerNode): string {
  return node.nameZh || node.name || node.cid;
}

/**
 * 按树结构拼「父 > 子 > 叶」的中文路径。
 *
 * 为什么不用 node.path：后端存的 path 来自妙手原始层级名，根节点是
 * 平台母语（如 `Sports & Outdoor/运动鞋/舞鞋`），中文界面里混着英文很难读。
 * 这里改成按树自己走一遍，每一级都取 nameZh（没有才退回 name）。
 */
function zhPathOf(targetCid: string, tree: PickerNode[]): string {
  const walk = (nodes: PickerNode[], trail: string[]): string | null => {
    for (const node of nodes) {
      const next = [...trail, labelOf(node)];
      if (node.cid === targetCid) return next.join(" / ");
      const hit = walk(node.children ?? [], next);
      if (hit) return hit;
    }
    return null;
  };
  return walk(tree, []) ?? "";
}

/** 只保留能被选中的节点（叶子且未禁用），用于搜索与最终选择。 */
function isSelectable(node: PickerNode): boolean {
  return node.isLeaf && !node.disabled;
}

export function CategoryPicker({
  tree,
  value,
  onChange,
  historyLimit = DEFAULT_HISTORY_LIMIT,
  className,
}: CategoryPickerProps) {
  // 三列各自高亮的节点 cid
  const [col1, setCol1] = useState<string | null>(null);
  const [col2, setCol2] = useState<string | null>(null);
  // 悬停展开（妙手是 hover 预览，点击才锁定）
  const [hoverCol1, setHoverCol1] = useState<string | null>(null);
  const [hoverCol2, setHoverCol2] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const initialized = useRef(false);

  useEffect(() => {
    setHistory(readHistory());
  }, []);

  // 从 value 反推三列的选中路径（打开时自动定位到当前类目）
  useEffect(() => {
    if (initialized.current) return;
    if (!value) {
      initialized.current = true;
      return;
    }
    for (const root of tree) {
      if (root.cid === value) {
        setCol1(root.cid);
        initialized.current = true;
        return;
      }
      for (const mid of root.children ?? []) {
        if (mid.cid === value) {
          setCol1(root.cid);
          setCol2(mid.cid);
          initialized.current = true;
          return;
        }
        for (const leaf of mid.children ?? []) {
          if (leaf.cid === value) {
            setCol1(root.cid);
            setCol2(mid.cid);
            initialized.current = true;
            return;
          }
        }
      }
    }
    initialized.current = true;
  }, [value, tree]);

  // 搜索：把命中的叶子摊平成单列结果，选中后直接结束
  const searchResults = useMemo(() => {
    const text = keyword.trim().toLowerCase();
    if (!text) return null;
    const hits: PickerNode[] = [];
    const walk = (nodes: PickerNode[]): void => {
      for (const node of nodes) {
        const label = `${node.nameZh ?? ""} ${node.name ?? ""} ${node.cid}`.toLowerCase();
        if (isSelectable(node) && label.includes(text)) hits.push(node);
        if (node.children?.length) walk(node.children);
      }
    };
    walk(tree);
    return hits.slice(0, 200);
  }, [keyword, tree]);

  const activeRoot = col1 ?? hoverCol1;
  const column2 = useMemo(() => {
    const root = tree.find((node) => node.cid === activeRoot);
    return root?.children ?? [];
  }, [tree, activeRoot]);

  const activeMid = col2 ?? hoverCol2;
  const column3 = useMemo(() => {
    const mid = column2.find((node) => node.cid === activeMid);
    return mid?.children ?? [];
  }, [column2, activeMid]);

  const choose = (node: PickerNode): void => {
    if (!isSelectable(node)) return;
    onChange(node.cid);
    const label = zhPathOf(node.cid, tree) || labelOf(node);
    const next = [
      { cid: node.cid, label },
      ...history.filter((item) => item.cid !== node.cid),
    ].slice(0, historyLimit);
    setHistory(next);
    writeHistory(next);
  };

  const currentLabel = useMemo(
    () => (value ? zhPathOf(value, tree) : ""),
    [tree, value],
  );

  /** 一列：「父级 > 子级」文字，右侧箭头表示还能往下钻。 */
  const renderRow = (
    node: PickerNode,
    level: 1 | 2 | 3,
    onPick: () => void,
    onHover: () => void,
  ) => {
    const selected =
      level === 1 ? col1 === node.cid
        : level === 2 ? col2 === node.cid
          : value === node.cid;
    const hasChildren = Boolean(node.children?.length);
    const selectable = isSelectable(node) || (level === 3 && node.isLeaf && !node.disabled);
    return (
      <button
        key={`${level}-${node.cid}`}
        type="button"
        onClick={onPick}
        onMouseEnter={onHover}
        disabled={!selectable && !hasChildren}
        className={cn(
          "flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-[13px] transition-colors",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-600/40",
          node.disabled
            ? "cursor-not-allowed text-fg-disabled"
            : selected
              ? "bg-brand-50 font-medium text-brand-700"
              : "text-fg hover:bg-canvas-inset",
        )}
        title={node.disabled ? `${labelOf(node)}（平台已禁用）` : labelOf(node)}
      >
        <span className="min-w-0 truncate">
          {labelOf(node)}
          {node.disabled && <span className="ml-1 text-2xs">（禁用）</span>}
        </span>
        {(hasChildren || (level < 3 && !node.isLeaf)) && (
          <ChevronRight
            className={cn("size-3.5 shrink-0", selected ? "text-brand-600" : "text-fg-muted")}
            aria-hidden
            strokeWidth={2}
          />
        )}
      </button>
    );
  };

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {/* 历史记录：最近选过的叶子，一键复用 */}
      {history.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="inline-flex items-center gap-1 text-2xs text-fg-muted">
            <Clock className="size-3" aria-hidden strokeWidth={1.75} />
            历史记录
          </span>
          {history.map((entry) => (
            <button
              key={entry.cid}
              type="button"
              onClick={() => onChange(entry.cid)}
              className={cn(
                "rounded-sm border px-2 py-0.5 text-2xs transition-colors",
                value === entry.cid
                  ? "border-brand-600/50 bg-brand-50 text-brand-700"
                  : "border-border bg-surface text-fg-secondary hover:border-brand-600/40 hover:text-fg",
              )}
              title={entry.label}
            >
              {entry.label.split("/").pop()?.trim() || entry.label}
            </button>
          ))}
        </div>
      )}

      {/* 搜索：命中时用单列结果替代三栏，选完自动收起 */}
      <div className="relative">
        <Search
          className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-fg-muted"
          aria-hidden
          strokeWidth={1.75}
        />
        <Input
          value={keyword}
          onChange={(event) => setKeyword(event.currentTarget.value)}
          placeholder="搜索类目名或 cid"
          className="h-8 pl-8 text-[13px]"
        />
      </div>

      {searchResults ? (
        <div className="max-h-[320px] overflow-y-auto rounded-md border border-border">
          {searchResults.length === 0 ? (
            <p className="p-3 text-xs text-fg-muted">没有匹配的类目</p>
          ) : (
            searchResults.map((node) => (
              <button
                key={node.cid}
                type="button"
                onClick={() => {
                  choose(node);
                  setKeyword("");
                }}
                className={cn(
                  "flex w-full items-center justify-between gap-2 border-b border-border-subtle px-3 py-2 text-left text-[13px] last:border-0",
                  value === node.cid
                    ? "bg-brand-50 font-medium text-brand-700"
                    : "text-fg hover:bg-canvas-inset",
                )}
              >
                <span className="min-w-0 truncate" title={zhPathOf(node.cid, tree) || labelOf(node)}>
                  {zhPathOf(node.cid, tree) || labelOf(node)}
                </span>
                <span className="shrink-0 font-mono text-2xs text-fg-muted">{node.cid}</span>
              </button>
            ))
          )}
        </div>
      ) : (
        // 三栏联动：每列独立滚动，高度固定，互不撑高
        <div className="grid grid-cols-3 gap-2">
          <div className="h-[320px] overflow-y-auto rounded-md border border-border">
            {tree.map((node) =>
              renderRow(
                node,
                1,
                () => {
                  setCol1(node.cid);
                  setCol2(null);
                  if (isSelectable(node)) choose(node);
                },
                () => setHoverCol1(node.cid),
              ),
            )}
          </div>
          <div className="h-[320px] overflow-y-auto rounded-md border border-border">
            {column2.length === 0 ? (
              <p className="p-3 text-2xs text-fg-muted">先选左侧父类目</p>
            ) : (
              column2.map((node) =>
                renderRow(
                  node,
                  2,
                  () => {
                    setCol2(node.cid);
                    if (isSelectable(node)) choose(node);
                  },
                  () => setHoverCol2(node.cid),
                ),
              )
            )}
          </div>
          <div className="h-[320px] overflow-y-auto rounded-md border border-border">
            {column3.length === 0 ? (
              <p className="p-3 text-2xs text-fg-muted">先选中间子类目</p>
            ) : (
              column3.map((node) =>
                renderRow(
                  node,
                  3,
                  () => choose(node),
                  () => {},
                ),
              )
            )}
          </div>
        </div>
      )}

      {/* 已选：让用户确认选到的是哪一支 */}
      <div className="flex items-center gap-2 border-t border-border-subtle pt-2 text-xs">
        <span className="shrink-0 text-fg-muted">当前选择：</span>
        {value ? (
          <span className="inline-flex min-w-0 items-center gap-1.5 text-brand-700">
            <Tag className="size-3.5 shrink-0" aria-hidden strokeWidth={1.75} />
            <span className="min-w-0 truncate" title={currentLabel}>{currentLabel}</span>
            <Badge variant="neutral" size="sm">{value}</Badge>
          </span>
        ) : (
          <span className="text-fg-muted">未选择（只能选最右列的叶子类目）</span>
        )}
      </div>
    </div>
  );
}

export default CategoryPicker;
