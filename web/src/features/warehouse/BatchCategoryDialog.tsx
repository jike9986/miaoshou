// features/warehouse —— 批量设置分类（api.md §4.5 POST /warehouse/batch-category）
// 两种目标：自定义分组（groupId）或平台类目（cid + name）。

import { useEffect, useMemo, useRef, useState } from "react";
import { FolderPlus, Tag } from "lucide-react";
import { apiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { isConflictBusy, revisionConflictOf, type RevisionConflict } from "@/lib/conflict";
import { isLanguageMismatch } from "@/lib/constants";
import { useBatchCategory, useCategoryTree, useCreateGroup, useGroups } from "@/lib/queries";
import type { BatchCategoryRequest, CategoryNode } from "@/lib/types";
import { RevisionConflictDialog } from "@/features/shared/RevisionConflictDialog";
import { CategoryPicker, type PickerNode } from "@/features/warehouse/CategoryPicker";
import {
  Button,
  Dialog,
  Field,
  InlineError,
  Input,
  SkeletonRows,
  Tabs,
  tabId,
  tabPanelId,
  toast,
} from "@/components/ui";

export type BatchTargetKind = "group" | "category";

export interface BatchCategoryDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  ids: string[];
  site?: string | null;
  /**
   * 妙手账号 id。类目缓存按账号隔离存（categories.account_id），
   * 不传就只能查全局哨兵 account_id=0 —— 真实数据都在某账号下，
   * 下拉会空着。
   */
  accountId?: string | null;
  connected: boolean;
  /**
   * 选中商品各自的当前版本号（id → revision）。
   * 传了就带 expectedRevisions：后端逐件比对，任何一件被别人改过就整体回滚并回 409。
   * 不传（老列表没有 revision 字段）就不带，行为与升级前一致。
   */
  revisions?: Record<string, number>;
  /** 「刷新看最新」时重新拉一次商品列表 */
  onRefresh?: () => void;
  onDone?: (updated: number, failed: number) => void;
}

/** 把类目树摊平成叶子列表（api.md §6.1 leafOnly 的本地等价实现） */
function flattenLeaves(nodes: CategoryNode[], result: CategoryNode[] = []): CategoryNode[] {
  for (const node of nodes) {
    if (node.isLeaf) {
      result.push(node);
    } else if (node.children.length > 0) {
      flattenLeaves(node.children, result);
    }
  }
  return result;
}

export function BatchCategoryDialog({
  open,
  onOpenChange,
  ids,
  site,
  accountId,
  connected,
  revisions,
  onRefresh,
  onDone,
}: BatchCategoryDialogProps) {
  const [tab, setTab] = useState<BatchTargetKind>("group");
  const [groupId, setGroupId] = useState<string | null>(null);
  const [cid, setCid] = useState<string | null>(null);
  const [newGroupName, setNewGroupName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<RevisionConflict | null>(null);
  // 冲突后点「保留我的修改」要原样重发同一个请求体
  const pendingBodyRef = useRef<BatchCategoryRequest | null>(null);

  const groupsQuery = useGroups();
  const createGroup = useCreateGroup();
  const batch = useBatchCategory();
  const treeQuery = useCategoryTree(
    { accountId: accountId ?? null, site: site ?? null, depth: 6, leafOnly: true },
    open && tab === "category" && connected
  );

  const groups = groupsQuery.data?.groups ?? [];
  const leaves = useMemo(
    () => flattenLeaves(treeQuery.data?.tree ?? []),
    [treeQuery.data?.tree]
  );

  useEffect(() => {
    if (!open) {
      setError(null);
      setNewGroupName("");
      setConflict(null);
      pendingBodyRef.current = null;
    }
  }, [open]);

  /**
   * 统一提交：save 与「保留我的修改」共用。
   * 带上 expectedRevisions 后，只要有一件商品被别人改过，后端就整体回滚并回 409，
   * 这时停下来问人 —— 不能自动重试，否则会把别人的改动一起盖掉。
   */
  const run = (body: BatchCategoryRequest, label: string): void => {
    pendingBodyRef.current = body;
    batch.mutate(body, {
      onSuccess: (result) => {
        pendingBodyRef.current = null;
        toast.success(`已把 ${result.updated} 件商品${label}`);
        onDone?.(result.updated, result.failed.length);
        onOpenChange(false);
      },
      onError: (mutationError) => {
        const found = revisionConflictOf(mutationError);
        if (found) {
          setConflict(found);
          return;
        }
        if (isConflictBusy(mutationError)) {
          toast.warning(apiErrorMessage(mutationError));
          return;
        }
        setError(apiErrorMessage(mutationError));
      },
    });
  };

  const submit = (): void => {
    setError(null);
    if (ids.length === 0) {
      setError("请先选择要设置分类的商品");
      return;
    }
    if (ids.length > 200) {
      setError("一次最多处理 200 件商品");
      return;
    }
    if (tab === "group") {
      if (!groupId) {
        setError("请选择一个分组");
        return;
      }
      run(
        { ids, groupId, ...(revisions ? { expectedRevisions: revisions } : {}) },
        "移到目标分组"
      );
      return;
    }

    if (!cid) {
      setError("请选择平台类目");
      return;
    }
    const leaf = leaves.find((node) => node.cid === cid);
    run(
      {
        ids,
        cid,
        name: leaf?.nameZh || leaf?.name || "",
        ...(revisions ? { expectedRevisions: revisions } : {}),
      },
      `设为「${leaf?.nameZh || leaf?.name || cid}」`
    );
  };

  /** 「刷新看最新」：关掉弹窗、重新拉列表，让用户按最新数据重选 */
  const refreshAfterConflict = (): void => {
    setConflict(null);
    pendingBodyRef.current = null;
    onRefresh?.();
    onOpenChange(false);
    toast.info("已重新拉取商品列表", {
      description: "这次没提交的改动已放弃，请确认最新内容后再设置分类。",
    });
  };

  /** 「保留我的修改」：仍按原请求重发一次（覆盖别人对这批商品的改动，由人显式授权） */
  const keepMineAfterConflict = (): void => {
    const body = pendingBodyRef.current;
    setConflict(null);
    if (!body) return;
    // 覆盖语义：去掉版本约束，让后端直接写入
    const { expectedRevisions: _ignored, ...withoutRevisions } = body;
    batch.mutate(withoutRevisions, {
      onSuccess: (result) => {
        pendingBodyRef.current = null;
        toast.success(`已把 ${result.updated} 件商品改到目标分类（覆盖了对方的改动）`);
        onDone?.(result.updated, result.failed.length);
        onOpenChange(false);
      },
      onError: (mutationError) => {
        const found = revisionConflictOf(mutationError);
        if (found) {
          setConflict(found);
          return;
        }
        if (isConflictBusy(mutationError)) {
          toast.warning(apiErrorMessage(mutationError));
          return;
        }
        setError(apiErrorMessage(mutationError));
      },
    });
  };

  const createGroupAndSelect = (): void => {
    const name = newGroupName.trim();
    if (!name) {
      setError("请填写分组名称");
      return;
    }
    setError(null);
    createGroup.mutate(
      { name },
      {
        onSuccess: (result) => {
          setGroupId(result.group.id);
          setNewGroupName("");
          toast.success(`已创建分组「${result.group.name}」`);
        },
        onError: (mutationError) => setError(apiErrorMessage(mutationError)),
      }
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      // xl（880px）：平台类目是三栏联动，md（480px）下每栏只有 140px，
      // 类目名被截成一两个字，看起来像「全挤到左列」（负责人 2026-11-11）。
      // 三栏每栏 280px 才够显示「运动与户外服装」「Casual Shoes」这种长名。
      size="xl"
      title="设置分类"
      description={`对已选的 ${ids.length} 件商品统一改分类。仓库分组只是你自己归类用；平台类目（运动鞋 / 运动拖鞋 / 轮滑鞋…）才是发布到平台时的类目。`}
      closeOnOverlay={false}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button variant="primary" loading={batch.isPending} onClick={submit}>
            应用到 {ids.length} 件
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Tabs
          value={tab}
          onChange={(value) => {
            setTab(value);
            setError(null);
          }}
          idBase="batch-category"
          items={[
            { value: "group", label: "仓库分组（自己建）", icon: <FolderPlus className="size-3.5" /> },
            { value: "category", label: "平台类目（运动鞋等）", icon: <Tag className="size-3.5" /> },
          ]}
        />

        {tab === "group" && (
          <div
            role="tabpanel"
            id={tabPanelId("batch-category", "group")}
            aria-labelledby={tabId("batch-category", "group")}
            className="contents"
          >
            {groupsQuery.isLoading ? (
              <SkeletonRows rows={3} />
            ) : (
              <div className="flex flex-col gap-3">
                <div className="flex items-baseline justify-between">
                  <Field label="目标分组">
                    <span />
                  </Field>
                  <span className="text-2xs text-fg-muted">
                    分组用于仓库内部归类，不影响平台类目
                  </span>
                </div>
                {/* 分组全部平铺（负责人 2026-11-11：下拉要展开还要滚动，
                    被弹窗边缘挡住；分组数量就几个，直接列出来最好用）。
                    点卡片选中，再点一次取消；新建分组输入框放在列表后面。 */}
                <div className="grid max-h-64 grid-cols-2 gap-2 overflow-y-auto rounded-md border border-border-subtle bg-canvas-inset/60 p-2 sm:grid-cols-3">
                  {groups.map((group) => {
                    const active = groupId === group.id;
                    return (
                      <button
                        key={group.id}
                        type="button"
                        onClick={() => setGroupId(active ? null : group.id)}
                        aria-pressed={active}
                        className={cn(
                          "flex min-h-[52px] flex-col justify-center rounded-md border px-3 py-2 text-left",
                          "transition-colors duration-fast ease-in-out",
                          active
                            ? "border-brand-600 bg-brand-50 text-brand-700 ring-1 ring-brand-600"
                            : "border-border bg-surface text-fg hover:border-border-strong"
                        )}
                      >
                        <span className="truncate text-xs font-medium">{group.name}</span>
                        <span className="num mt-0.5 text-2xs text-fg-muted">{group.count} 件</span>
                      </button>
                    );
                  })}
                  {groups.length === 0 && (
                    <p className="col-span-full text-center text-xs text-fg-muted">
                      还没有分组，在下面创建一个
                    </p>
                  )}
                </div>

                <div className="rounded-md border border-border-subtle bg-canvas-inset/60 p-3">
                  <p className="text-xs font-medium text-fg-secondary">新建分组</p>
                  <div className="mt-2 flex items-center gap-2">
                    <Input
                      value={newGroupName}
                      maxLength={64}
                      placeholder="如：春季新款"
                      onChange={(event) => setNewGroupName(event.currentTarget.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") createGroupAndSelect();
                      }}
                    />
                    <Button
                      variant="secondary"
                      loading={createGroup.isPending}
                      onClick={createGroupAndSelect}
                    >
                      创建并选中
                    </Button>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {tab === "category" && (
          <div
            role="tabpanel"
            id={tabPanelId("batch-category", "category")}
            aria-labelledby={tabId("batch-category", "category")}
            className="contents"
          >
            {!connected && (
              <InlineError>未连接妙手时无法拉取平台类目树，请先到「连接妙手」页连接。</InlineError>
            )}
            {connected && treeQuery.isLoading && <SkeletonRows rows={3} />}
            {connected && treeQuery.isError && (
              <InlineError onRetry={() => void treeQuery.refetch()}>
                {apiErrorMessage(treeQuery.error)}
              </InlineError>
            )}
            {connected && !treeQuery.isLoading && !treeQuery.isError && (
              <div className="flex flex-col gap-1.5">
                <span className="text-xs font-medium text-fg-secondary">目标类目</span>
                {treeQuery.data?.stale && (
                  <p className="text-2xs text-warning-700">
                    类目缓存超过 7 天且本次回源失败，展示的是旧树
                  </p>
                )}
                <CategoryPicker
                  tree={(treeQuery.data?.tree ?? []) as PickerNode[]}
                  value={cid}
                  onChange={setCid}
                />
              </div>
            )}
          </div>
        )}

        {error && <InlineError>{error}</InlineError>}
      </div>

      {/* 批量场景的冲突：可能只是其中一件被别人改过，仍然要停下来问 */}
      <RevisionConflictDialog
        conflict={conflict}
        onOpenChange={(next) => {
          if (!next) setConflict(null);
        }}
        onRefresh={refreshAfterConflict}
        onKeepMine={keepMineAfterConflict}
        busy={batch.isPending}
        subject="这批商品"
      />
    </Dialog>
  );
}

/** 语言与站点不匹配时的兜底校验（发布页复用） */
export { isLanguageMismatch };

export default BatchCategoryDialog;
