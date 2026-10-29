// features/warehouse —— 成品仓库（核心页，api.md §4 / §5）
// 顶部 KPI · 分类栏 · 工具栏（搜索/每页/排序/刷新/设置分类）· 商品网格 · 多选 · 分页 · 浮动操作条。
// 批次编排不单独占导航项：作为本页第二个 tab 挂在商品列表旁边（后端 /batch 的
// 输入就是仓库商品的 detailId，放在一起才不用来回跳）。

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Calculator,
  CheckSquare,
  Inbox,
  Layers,
  ListChecks,
  PackageCheck,
  PackageSearch,
  Percent,
  RefreshCw,
  SearchX,
  Send,
  Trash2,
  Link2,
  Zap,
} from "lucide-react";
import { cn, formatCount } from "@/lib/utils";
import { api, ApiError, apiErrorMessage } from "@/lib/api";
import { LIMITS, toPublishItems } from "@/lib/constants";
import { listItem } from "@/lib/motion";
import { useOperatorIdentity } from "@/lib/operator";
import {
  useAuthStatus,
  useBatchDelete,
  useCreateGroup,
  useDeleteGroup,
  useGroups,
  useAutomationResults,
  useImportAutomationResults,
  useOperators,
  usePublish,
  useWarehouseList,
} from "@/lib/queries";
import type { GroupFacet, Product } from "@/lib/types";
import { useSessionStore, useWarehouseStore } from "@/features/stores";
import { Pagination, TextAction } from "@/features/shared";
import { PageContent, PageHeader } from "@/components/layout";
import { CarrierPage } from "@/features/carrier/CarrierPage";
import {
  Badge,
  BulkActionBar,
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  IconButton,
  Input,
  Select,
  SkeletonCardGrid,
  StatusDot,
  Tabs,
  tabId,
  tabPanelId,
  toast,
} from "@/components/ui";
import { CategoryBar } from "@/features/warehouse/CategoryBar";
import { ProductCard } from "@/features/warehouse/ProductCard";
import { RemoteImage } from "@/features/shared/RemoteImage";
import { BatchCategoryDialog } from "@/features/warehouse/BatchCategoryDialog";
import { CreateBatchDialog } from "@/features/warehouse/CreateBatchDialog";
import { ProductDetailDrawer } from "@/features/warehouse/ProductDetailDrawer";
import { PricePreviewDialog } from "@/features/warehouse/PricePreviewDialog";
import { TransformDialog } from "@/features/warehouse/TransformDialog";
import { PublishDialog } from "@/features/publish/PublishDialog";
import { BatchPanel } from "@/features/batch/BatchPanel";

const PAGE_SIZE_OPTIONS = [24, 48, 72, 100];

type WarehouseTab = "products" | "batch" | "automation";

export function WarehousePage() {
  const navigate = useNavigate();
  const reduced = useReducedMotion();
  const identity = useOperatorIdentity();
  const isMember = identity?.role === "member";

  // tab 记在 URL 上（?tab=batch）：建完批次后 toast 让用户「去批次标签看进度」，
  // 刷新一下回到商品列表会让人以为进度丢了。
  const [searchParams, setSearchParams] = useSearchParams();
  const tabValue = searchParams.get("tab");
  const tab: WarehouseTab = !isMember && (tabValue === "batch" || tabValue === "automation")
    ? tabValue
    : "products";
  const setTab = useCallback(
    (value: WarehouseTab) => {
      const next = new URLSearchParams(searchParams);
      if (value === "products") next.delete("tab");
      else next.set("tab", value);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams]
  );

  const query = useWarehouseStore();
  const clearSelected = query.clearSelected;
  const queryClient = useQueryClient();
  const currentShopId = useSessionStore((state) => state.currentShopId);
  const { data: auth } = useAuthStatus();
  const connected = auth?.connected ?? false;
  const operatorsQuery = useOperators({ enabled: tab === "automation" && !isMember });
  const isTeacher = !isMember && (identity?.role
    ? ["teacher", "admin"].includes(identity.role)
    : ["teacher", "admin"].includes(operatorsQuery.data?.current?.role ?? ""));
  const automationPage = Number(searchParams.get("automationPage") || "1");
  const automationQuery = useAutomationResults(automationPage, tab === "automation" && isTeacher);
  const importAutomationResults = useImportAutomationResults();
  const [automationSelected, setAutomationSelected] = useState<string[]>([]);

  const groupsQuery = useGroups();
  const createGroup = useCreateGroup();
  const deleteGroup = useDeleteGroup();
  const batchDelete = useBatchDelete();

  const category = query.category;
  // 搜索框已按界面精简要求移除；q 不再来自本页，避免旧状态残留影响列表
  const listQuery = useWarehouseList({
    page: query.page,
    pageSize: query.pageSize,
    q: null,
    claimed: query.claimed,
    status: query.status,
    sort: query.sort,
    cid: category.kind === "cid" ? category.cid : null,
    categoryId: category.kind === "categoryId" ? category.categoryId : null,
    groupId: category.kind === "group" ? category.groupId : null,
  });

  const data = listQuery.data;
  const items = useMemo(() => data?.items ?? [], [data?.items]);
  const facets = data?.facets;
  const total = data?.total ?? 0;

  const [createOpen, setCreateOpen] = useState(false);
  const [groupName, setGroupName] = useState("");
  const [groupError, setGroupError] = useState<string | null>(null);
  const [deleteGroupTarget, setDeleteGroupTarget] = useState<GroupFacet | null>(null);
  const [batchOpen, setBatchOpen] = useState(false);
  const [transformOpen, setTransformOpen] = useState(false);
  const [priceOpen, setPriceOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [publishIds, setPublishIds] = useState<string[]>([]);
  const [createBatchOpen, setCreateBatchOpen] = useState(false);
  // 发布弹窗要靠来源 shopId 判断这单是不是跨店（跨店要建载体，提前告知）
  const sourceShopIds = useMemo(() => {
    const map: Record<string, string> = {};
    for (const item of items) map[String(item.id)] = String(item.shopId ?? "");
    return map;
  }, [items]);

  const [carrierOpen, setCarrierOpen] = useState(false);
  const claimSharedProduct = useMutation({
    mutationFn: async (ids: string[]) => {
      let claimed = 0;
      const failures: string[] = [];
      for (const id of ids) {
        try {
          await api.post(`/warehouse/${encodeURIComponent(id)}/claim`);
          claimed += 1;
        } catch (error) {
          failures.push(apiErrorMessage(error));
        }
      }
      return { claimed, failures };
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["warehouse"] });
    },
  });
  // 认领到采集箱走 POST /publish + claimOnly=true（老版同样行为：只认领不发布）
  const claimOnly = usePublish();
  const selectedIds = query.selected;
  const selectedCount = selectedIds.length;
  const pageIds = items.map((item) => item.id);
  const selectedItems = items.filter((item) => selectedIds.includes(item.id));
  const memberClaimableIds = selectedItems.filter((item) => !item.claimed).map((item) => item.id);
  const memberPublishableIds = selectedItems.filter((item) => item.claimed).map((item) => item.id);
  // 选中商品各自的当前版本号：批量改分类时带上，任何一件被别人改过就整体回滚
  // 老后端列表不回 revision → 这个 map 是空的 → 请求里不带 expectedRevisions
  const selectedRevisions = useMemo(() => {
    const map: Record<string, number> = {};
    for (const item of items) {
      if (typeof item.revision === "number" && selectedIds.includes(item.id)) {
        map[item.id] = item.revision;
      }
    }
    return map;
  }, [items, selectedIds]);
  const hasSelectedRevisions = Object.keys(selectedRevisions).length > 0;
  const allSelectedOnPage = pageIds.length > 0 && pageIds.every((id) => selectedIds.includes(id));
  const someSelectedOnPage = pageIds.some((id) => selectedIds.includes(id));

  const toggleAllOnPage = useCallback(() => {
    if (allSelectedOnPage) {
      query.deselectMany(pageIds);
      return;
    }
    query.selectMany(pageIds);
  }, [allSelectedOnPage, pageIds, query]);

  const claimSelectedForMember = async (): Promise<void> => {
    const { claimed, failures } = await claimSharedProduct.mutateAsync(memberClaimableIds);
    if (failures.length > 0) {
      toast.warning(`已认领 ${claimed} 件，${failures.length} 件失败`, {
        description: failures[0],
      });
    } else {
      toast.success(`已认领 ${claimed} 件成品`);
    }
  };

  useEffect(() => {
    void queryClient.removeQueries({ queryKey: ["warehouse"] });
    void queryClient.removeQueries({ queryKey: ["system", "templates"] });
    clearSelected();
  }, [identity?.id, identity?.role, clearSelected, queryClient]);

  const openPublish = (ids: string[]): void => {
    if (!connected) {
      toast.warning(isMember ? "发布前请先绑定你自己的妙手 key" : "发布需要先连接妙手账号");
      navigate("/unlock", { state: { from: "/warehouse" } });
      return;
    }
    setPublishIds(ids);
    setPublishOpen(true);
  };

  const confirmDelete = (): void => {
    batchDelete.mutate(selectedIds, {
      onSuccess: (result) => {
        const failed = result.failures.length;
        if (failed > 0) {
          toast.warning(`已删除 ${result.deletedCount} 件，${failed} 件失败`, {
            description: result.failures[0]?.reason,
          });
        } else {
          toast.success(`已删除 ${result.deletedCount} 件商品`);
        }
        query.clearSelected();
        setDeleteOpen(false);
      },
      onError: (error) => {
        toast.error("批量删除失败", { description: apiErrorMessage(error) });
      },
    });
  };

  const submitCreateGroup = (): void => {
    const name = groupName.trim();
    if (!name) {
      setGroupError("请填写分组名称");
      return;
    }
    if (Array.from(name).length > LIMITS.groupNameLength) {
      setGroupError(`分组名最多 ${LIMITS.groupNameLength} 个字符`);
      return;
    }
    setGroupError(null);
    createGroup.mutate(
      { name },
      {
        onSuccess: (result) => {
          toast.success(`已创建分组「${result.group.name}」`);
          setCreateOpen(false);
          setGroupName("");
          query.setCategory({ kind: "group", groupId: result.group.id, name: result.group.name });
        },
        onError: (error) => {
          const code = error instanceof ApiError ? error.code : null;
          setGroupError(
            code === "DUPLICATE_NAME" ? "同名分组已存在，请换一个名字" : apiErrorMessage(error)
          );
        },
      }
    );
  };

  const confirmDeleteGroup = (): void => {
    const target = deleteGroupTarget;
    if (!target) return;
    deleteGroup.mutate(target.id, {
      onSuccess: (result) => {
        toast.success(`已删除分组「${target.name}」`, {
          description: `${result.productsUnassigned} 件商品已移出分组`,
        });
        if (category.kind === "group" && category.groupId === target.id) {
          query.setCategory({ kind: "all" });
        }
        setDeleteGroupTarget(null);
      },
      onError: (error) => {
        toast.error("删除分组失败", { description: apiErrorMessage(error) });
        setDeleteGroupTarget(null);
      },
    });
  };

  const refreshAll = (): void => {
    void listQuery.refetch();
    void groupsQuery.refetch();
  };

  const headerActions = (
    <>
      <IconButton
        label="刷新"
        size="md"
        variant="secondary"
        icon={<RefreshCw className={cn(listQuery.isFetching && "animate-spin")} />}
        onClick={refreshAll}
      />
      {/* 跨店发布辅助：同一个商品发到不同店铺时，需要一张「载体链接」来重建。
          这个名字太技术了，改成用户能懂的说法。 */}
      {!isMember && <Button
        variant="secondary"
        size="md"
        icon={<Link2 />}
        title="同一个商品要发到不同店铺时，需要先生成载体链接。没有链接时跨店发布会失败。"
        onClick={() => setCarrierOpen(true)}
      >
        跨店发布链接
      </Button>}
    </>
  );

  const carrierDialog = (
    <Dialog
      open={carrierOpen}
      onOpenChange={setCarrierOpen}
      size="lg"
      title="跨店发布链接"
      description="同一个商品要发到不同店铺时，需要先生成载体链接。池子空了跨店发布会失败。"
    >
      <CarrierPage embedded />
    </Dialog>
  );

  return (
    <>
      <PageHeader
        title="成品仓库"
        actions={headerActions}
      />

      <PageContent width="table">
        {/* 批次编排不新增侧边栏导航项：作为本页第二个 tab。
            批次面板卸载时它的自动推进定时器会被清掉（见 BatchPanel 的 useEffect）。 */}
        <Tabs
          value={tab}
          onChange={(value) => setTab(value as WarehouseTab)}
          idBase="warehouse"
          items={[
            { value: "products", label: "商品列表", icon: <PackageSearch className="size-3.5" /> },
            ...(!isMember ? [
              { value: "automation", label: "自动化结果", icon: <Zap className="size-3.5" /> },
              { value: "batch", label: "批次", icon: <ListChecks className="size-3.5" /> },
            ] as const : []),
          ]}
        />

        {tab === "batch" && (
          <div
            role="tabpanel"
            id={tabPanelId("warehouse", "batch")}
            aria-labelledby={tabId("warehouse", "batch")}
          >
            <BatchPanel
              accountId={auth?.accountId ?? null}
              onGoToProducts={() => setTab("products")}
            />
          </div>
        )}

        {tab === "automation" && (
          <div role="tabpanel" id={tabPanelId("warehouse", "automation")} aria-labelledby={tabId("warehouse", "automation")} className="mt-4">
            {!isTeacher && !operatorsQuery.isLoading && (
              <Card><EmptyState title="仅老师和管理员可查看自动化结果" minHeight={180} /></Card>
            )}
            {isTeacher && automationQuery.isLoading && <SkeletonCardGrid count={6} />}
            {isTeacher && automationQuery.isError && (
              <ErrorState title="自动化结果加载失败" description={apiErrorMessage(automationQuery.error)} onRetry={() => void automationQuery.refetch()} retrying={automationQuery.isFetching} />
            )}
            {isTeacher && !automationQuery.isLoading && !automationQuery.isError && automationQuery.data?.items.length === 0 && (
              <Card><EmptyState title="暂无待入仓的自动化结果" description="自动化处理完成的商品会先保存在这里，经老师选择后才会加入成品仓库。" minHeight={220} /></Card>
            )}
            {isTeacher && automationQuery.data && automationQuery.data.items.length > 0 && (
              <>
                <div className="mb-3 flex items-center gap-2 text-xs text-fg-muted">
                  <Checkbox
                    checked={automationQuery.data.items.every((item) => automationSelected.includes(item.id))}
                    onChange={() => {
                      const ids = automationQuery.data?.items.map((item) => item.id) ?? [];
                      const allSelected = ids.length > 0 && ids.every((id) => automationSelected.includes(id));
                      setAutomationSelected((selected) => allSelected
                        ? selected.filter((id) => !ids.includes(id))
                        : [...new Set([...selected, ...ids])]);
                    }}
                    aria-label="全选当前页自动化结果"
                  />
                  <span>本页 {formatCount(automationQuery.data.items.length)} 件</span>
                </div>
                <div className="divide-y divide-border-subtle border-y border-border-subtle">
                  {automationQuery.data.items.map((item) => (
                    <div key={item.id} className="grid grid-cols-[20px_56px_minmax(0,1fr)] items-center gap-3 py-3 sm:grid-cols-[20px_56px_minmax(0,1fr)_auto]">
                      <Checkbox
                        checked={automationSelected.includes(item.id)}
                        onChange={() => setAutomationSelected((selected) => selected.includes(item.id)
                          ? selected.filter((id) => id !== item.id)
                          : [...selected, item.id])}
                        aria-label={`选择 ${item.title || item.detailId}`}
                      />
                      <div className="size-14 overflow-hidden rounded-sm bg-canvas-inset"><RemoteImage src={item.image} className="size-full" /></div>
                      <div className="min-w-0">
                        <p className="line-clamp-2 text-sm font-medium text-fg">{item.title || "（无标题）"}</p>
                        <p className="mt-1 truncate text-xs text-fg-muted">{item.shopName || item.shopId} · {item.detailId}</p>
                      </div>
                      <span className="hidden text-xs text-fg-muted sm:block">{item.status === "published" ? "已提交发布" : "处理完成"}</span>
                    </div>
                  ))}
                </div>
                <Pagination
                  page={automationPage}
                  pageSize={24}
                  total={automationQuery.data.total}
                  hasMore={automationQuery.data.hasMore}
                  showTotal
                  onPageChange={(page) => {
                    const next = new URLSearchParams(searchParams);
                    if (page === 1) next.delete("automationPage");
                    else next.set("automationPage", String(page));
                    setSearchParams(next, { replace: true });
                    setAutomationSelected([]);
                  }}
                  disabled={automationQuery.isFetching}
                />
              </>
            )}
          </div>
        )}

        {tab === "products" && (
          <div
            role="tabpanel"
            id={tabPanelId("warehouse", "products")}
            aria-labelledby={tabId("warehouse", "products")}
            className="contents"
          >
            {/* 分类栏 */}
            {!isMember && <CategoryBar
              categories={facets?.categories ?? []}
              groups={facets?.groups ?? groupsQuery.data?.groups.map((group) => ({
                id: group.id,
                name: group.name,
                color: group.color,
                count: group.count,
              })) ?? []}
              active={category}
              total={total}
              loading={listQuery.isLoading}
              onSelect={(filter) => query.setCategory(filter)}
              onCreateGroup={() => {
                setGroupName("");
                setGroupError(null);
                setCreateOpen(true);
              }}
              onDeleteGroup={(group) => setDeleteGroupTarget(group)}
            />}

            {/* 工具栏 */}
            <div className="flex flex-wrap items-center gap-2">
              <Select
                size="md"
                aria-label="认领状态"
                className="w-[132px]"
                value={query.claimed === null ? "all" : query.claimed ? "claimed" : "unclaimed"}
                onChange={(value) =>
                  query.setClaimed(value === "all" ? null : value === "claimed")
                }
                options={[
                  { value: "all", label: "全部状态" },
                  { value: "claimed", label: "已认领" },
                  { value: "unclaimed", label: "未认领" },
                ]}
              />

              {/* 全选 + 每页条数放同一行右侧：切到每页 100 条时不用滑到列表底部才改得动 */}
              <div className="ml-auto flex items-center gap-2">
                <Select
                  size="md"
                  aria-label="每页条数"
                  className="w-[76px] shrink-0"
                  value={String(query.pageSize)}
                  onChange={(value) => query.setPageSize(Number(value))}
                  options={PAGE_SIZE_OPTIONS.map((size) => ({
                    value: String(size),
                    label: String(size),
                  }))}
                />

                <Button
                  variant="secondary"
                  size="md"
                  icon={<CheckSquare />}
                  className="shrink-0"
                  onClick={toggleAllOnPage}
                  disabled={pageIds.length === 0}
                  title={allSelectedOnPage ? "取消本页全部选中" : "选中本页全部商品"}
                >
                  {allSelectedOnPage ? "取消本页全选" : "全选本页"}
                  {selectedCount > 0 && <span className="num text-fg-muted">（{selectedCount}）</span>}
                </Button>
              </div>
            </div>

            {(query.claimed !== null || category.kind !== "all") && (
              <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
                <span>当前筛选：</span>
                {query.claimed !== null && (
                  <Badge variant="neutral">{query.claimed ? "已认领" : "未认领"}</Badge>
                )}
                {category.kind !== "all" && <Badge variant="brand">{category.name}</Badge>}
                <TextAction
                  onClick={() => {
                    query.setClaimed(null);
                    query.setCategory({ kind: "all" });
                    query.setStatus(null);
                  }}
                >
                  清除筛选
                </TextAction>
              </div>
            )}

            {/* 列表主体 */}
            {listQuery.isLoading && <SkeletonCardGrid count={query.pageSize > 24 ? 12 : 8} />}

            {listQuery.isError && (
              <Card>
                <ErrorState
                  title="仓库加载失败"
                  description={apiErrorMessage(listQuery.error)}
                  errorCode={listQuery.error instanceof ApiError ? listQuery.error.code : null}
                  onRetry={() => void listQuery.refetch()}
                  retrying={listQuery.isFetching}
                />
              </Card>
            )}

            {!listQuery.isLoading && !listQuery.isError && items.length === 0 && (
              <Card>
                <EmptyState
                  icon={<SearchX strokeWidth={1.5} />}
                  title="仓库还是空的"
                  description="在采集箱里处理商品后，成品会自动进入这里。"
                  action={{ label: "去采集箱", onClick: () => navigate("/collect") }}
                />
              </Card>
            )}

            {!listQuery.isLoading && !listQuery.isError && items.length > 0 && (
              <>
                <div className="flex items-center gap-2 text-xs text-fg-muted">
                  <button
                    type="button"
                    onClick={toggleAllOnPage}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-sm px-1 py-0.5 transition-colors duration-fast",
                      "hover:bg-surface-active focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32"
                    )}
                  >
                    <StatusDot tone={allSelectedOnPage ? "success" : someSelectedOnPage ? "warning" : "muted"} />
                    {allSelectedOnPage ? "取消本页全选" : "全选本页"}
                  </button>
                  <span aria-hidden>·</span>
                  <span className="num">本页 {formatCount(items.length)} 件</span>
                  {listQuery.isFetching && (
                    <span className="flex items-center gap-1 text-fg-muted">
                      <RefreshCw className="size-3 animate-spin" />
                      正在刷新
                    </span>
                  )}
                </div>

                <motion.div
                  initial={reduced ? undefined : "hidden"}
                  animate={reduced ? undefined : "show"}
                  className={cn(
                    "grid gap-4",
                    "grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5",
                    listQuery.isFetching && "opacity-90"
                  )}
                >
                  <AnimatePresence initial={false}>
                    {items.map((product: Product, index: number) => (
                      <motion.div
                        key={product.id}
                        variants={reduced ? undefined : listItem}
                        custom={index}
                        layout={false}
                      >
                        <ProductCard
                          product={product}
                          isMember={isMember}
                          index={index}
                          selected={selectedIds.includes(product.id)}
                          onToggle={query.toggleSelected}
                          onOpen={(item) => setDetailId(item.id)}
                        />
                      </motion.div>
                    ))}
                  </AnimatePresence>
                </motion.div>
              </>
            )}

            <Pagination
              page={query.page}
              pageSize={query.pageSize}
              total={total}
              hasMore={data?.hasMore}
              // 页头统计卡已经显示了「共 355 件」，分页器再写一遍「共 N 条」
              // 就是同一数字出现两次，用户明确要求去掉。
              showTotal={false}
              pageSizeOptions={PAGE_SIZE_OPTIONS.map((size) => ({ value: size, label: String(size) }))}
              onPageChange={(page) => {
                if (isMember) query.clearSelected();
                query.setPage(page);
              }}
              onPageSizeChange={query.setPageSize}
              disabled={listQuery.isFetching}
            />
          </div>
        )}
      </PageContent>
      {carrierDialog}

      {/* 浮动批量操作条（design-system §5.8）：只在商品列表 tab 出现，
          看批次时还飘着「已选 12 件」会让人以为选的是批次 */}
      <BulkActionBar
        count={tab === "automation" ? automationSelected.length : tab === "products" ? selectedCount : 0}
        total={tab === "automation" ? automationQuery.data?.total : total}
        unit="件"
        onClear={() => tab === "automation" ? setAutomationSelected([]) : query.clearSelected()}
        actions={tab === "automation" ? [{
            label: importAutomationResults.isPending ? "正在加入…" : "加入成品仓库",
            icon: <PackageCheck className="size-3.5" />,
            disabled: importAutomationResults.isPending,
            onClick: () => importAutomationResults.mutate(automationSelected, {
              onSuccess: (result) => {
                setAutomationSelected([]);
                toast.success(`已加入成品仓库 ${formatCount(result.added)} 件`);
              },
              onError: (error) => toast.error("加入成品仓库失败", { description: apiErrorMessage(error) }),
            }),
          }] : isMember ? [
          ...(memberClaimableIds.length > 0 ? [{
            label: claimSharedProduct.isPending
              ? "正在认领…"
              : `认领成品 ${memberClaimableIds.length} 件`,
            icon: <PackageCheck className="size-3.5" />,
            disabled: claimSharedProduct.isPending,
            onClick: () => void claimSelectedForMember(),
          }] : []),
          ...(memberPublishableIds.length > 0 ? [{
            label: "发布已认领商品",
            icon: <Send className="size-3.5" />,
            onClick: () => openPublish(memberPublishableIds),
          }] : []),
        ] : [
          {
            label: "认领到采集箱",
            icon: <Inbox className="size-3.5" />,
            onClick: () => {
              if (!connected) {
                toast.warning("认领需要先连接妙手账号");
                navigate("/unlock", { state: { from: "/warehouse" } });
                return;
              }
              if (!currentShopId) {
                toast.warning("请先选择要认领到的店铺");
                return;
              }
              // 老版行为：原地调接口把这批商品放进店铺采集箱，不跳页、不发布。
              // 跨店商品由后端自动建载体（publish_service 的 claimOnly 分支）。
              claimOnly.mutate(
                {
                  items: toPublishItems(selectedIds.map((id) => ({ warehouseId: id }))),
                  shopId: currentShopId,
                  claimOnly: true,
                  confirm: true,
                },
                {
                  onSuccess: (result) => {
                    const done = result.claimed?.length ?? 0;
                    const failed = result.failures?.length ?? 0;
                    if (failed > 0) {
                      toast.warning(
                        `已认领 ${done} 件，${failed} 件失败`
                      );
                    } else {
                      toast.success(`已认领 ${done} 件到采集箱（未发布）`);
                    }
                    query.clearSelected();
                  },
                  onError: (error) => toast.error(apiErrorMessage(error)),
                }
              );
            },
          },
          {
            label: "创建批次",
            icon: <ListChecks className="size-3.5" />,
            onClick: () => setCreateBatchOpen(true),
          },
          {
            label: "价格试算",
            icon: <Calculator className="size-3.5" />,
            onClick: () => setPriceOpen(true),
          },
          {
            label: "批量改价",
            icon: <Percent className="size-3.5" />,
            onClick: () => setTransformOpen(true),
          },
          {
            label: "设置分类",
            icon: <Layers className="size-3.5" />,
            onClick: () => setBatchOpen(true),
          },
          {
            label: "发布",
            icon: <Send className="size-3.5" />,
            onClick: () => openPublish(selectedIds),
          },
          {
            label: "删除",
            icon: <Trash2 className="size-3.5" />,
            danger: true,
            onClick: () => setDeleteOpen(true),
          },
        ]}
      />

      {/* 新建分类 */}
      <Dialog
        open={createOpen}
        onOpenChange={(open) => {
          setCreateOpen(open);
          if (!open) {
            setGroupName("");
            setGroupError(null);
          }
        }}
        size="sm"
        title="新建分类"
        description="分类（自定义分组）只用于仓库内部归类，不影响平台类目。"
        closeOnOverlay={false}
        footer={
          <>
            <Button variant="secondary" onClick={() => setCreateOpen(false)}>
              取消
            </Button>
            <Button variant="primary" loading={createGroup.isPending} onClick={submitCreateGroup}>
              创建
            </Button>
          </>
        }
      >
        <Field
          label="分类名称"
          required
          error={groupError}
          hint={`1–${LIMITS.groupNameLength} 个字符，名称唯一（大小写不敏感）`}
        >
          <Input
            value={groupName}
            maxLength={LIMITS.groupNameLength}
            autoFocus
            placeholder="如：春季新款"
            invalid={Boolean(groupError)}
            onChange={(event) => {
              setGroupName(event.currentTarget.value);
              setGroupError(null);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") submitCreateGroup();
            }}
          />
        </Field>
      </Dialog>

      {/* 删除分组 */}
      <ConfirmDialog
        open={deleteGroupTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteGroupTarget(null);
        }}
        title={`删除分组「${deleteGroupTarget?.name ?? ""}」？`}
        description="组内商品不会被删除，只会移出该分组。"
        confirmLabel="删除分组"
        danger
        loading={deleteGroup.isPending}
        onConfirm={confirmDeleteGroup}
      >
        {deleteGroupTarget && (
          <p className="text-[13px] leading-5 text-fg-secondary">
            当前该分组下有 <span className="num font-medium">{formatCount(deleteGroupTarget.count)}</span> 件商品。
          </p>
        )}
      </ConfirmDialog>

      <TransformDialog
        open={transformOpen}
        ids={selectedIds}
        revisions={hasSelectedRevisions ? selectedRevisions : undefined}
        onRefresh={() => void listQuery.refetch()}
        onClose={() => setTransformOpen(false)}
        onApplied={(result) => {
          toast.success(`已改价 ${formatCount(result.changedCount)} 件商品`);
          query.clearSelected();
        }}
      />

      {/* 批量删除 */}
      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={`删除已选的 ${selectedCount} 件商品？`}
        description="软删除：商品从仓库列表移除，发布记录保留。此操作不可逆。"
        confirmLabel={batchDelete.isPending ? "正在删除…" : `删除 ${selectedCount} 件`}
        danger
        loading={batchDelete.isPending}
        onConfirm={confirmDelete}
      >
        <p className="text-[13px] leading-5 text-fg-secondary">
          单个商品若有图片任务或发布在途中会删除失败，失败项会保留在列表里。
        </p>
      </ConfirmDialog>

      <PricePreviewDialog
        open={priceOpen}
        onOpenChange={setPriceOpen}
        ids={selectedIds}
      />

      {!isMember && <BatchCategoryDialog
        open={batchOpen}
        onOpenChange={setBatchOpen}
        ids={selectedIds}
        site={auth?.accounts.find((account) => account.id === auth?.accountId)?.site ?? null}
        connected={connected}
        revisions={hasSelectedRevisions ? selectedRevisions : undefined}
        onRefresh={() => void listQuery.refetch()}
        onDone={() => query.clearSelected()}
      />}

      {!isMember && <CreateBatchDialog
        open={createBatchOpen}
        onOpenChange={setCreateBatchOpen}
        ids={selectedIds}
        products={items}
        accountId={auth?.accountId ?? null}
        onCreated={() => {
          query.clearSelected();
          // 建完直接把用户带到批次标签，进度就在眼前，不用自己去找
          setTab("batch");
        }}
      />}

      <ProductDetailDrawer
        productId={detailId}
        open={detailId !== null}
        onOpenChange={(open) => {
          if (!open) setDetailId(null);
        }}
        onRequestDelete={(id) => {
          query.clearSelected();
          query.toggleSelected(id);
          setDeleteOpen(true);
          setDetailId(null);
        }}
        isMember={isMember}
      />

      <PublishDialog
        open={publishOpen}
        onOpenChange={setPublishOpen}
        warehouseIds={publishIds}
        sourceShopIds={sourceShopIds}
        defaultShopId={currentShopId}
        connected={connected}
        allowClaimOnly={!isMember}
        onSubmitted={() => query.clearSelected()}
      />
    </>
  );
}

export default WarehousePage;
