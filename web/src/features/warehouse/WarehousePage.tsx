// features/warehouse —— 成品仓库（核心页，api.md §4 / §5）
// 顶部 KPI · 分类栏 · 工具栏（搜索/每页/排序/刷新/设置分类）· 商品网格 · 多选 · 分页 · 浮动操作条。
// 批次编排不单独占导航项：作为本页第二个 tab 挂在商品列表旁边（后端 /batch 的
// 输入就是仓库商品的 detailId，放在一起才不用来回跳）。

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckSquare,
  ChevronLeft,
  ChevronRight,
  Globe,
  Inbox,
  Layers,
  ListChecks,
  PackageCheck,
  PackageSearch,
  RefreshCw,
  Rocket,
  SearchX,
  Send,
  Trash2,
  Zap,
} from "lucide-react";
import { cn, formatCount } from "@/lib/utils";
import { api, ApiError, apiErrorMessage } from "@/lib/api";
import { LIMITS, toPublishItems } from "@/lib/constants";
import { listItem } from "@/lib/motion";
import { useOperatorIdentity } from "@/lib/operator";
import { getOperatorSessionEpoch, runIfOperatorSessionCurrent } from "@/lib/operatorSessionEpoch.mjs";
import { queryKeys } from "@/lib/queryKeys";
import {
  useAuthStatus,
  useBatchDelete,
  useCreateGroup,
  useDeleteGroup,
  useGroups,
  useReorderGroup,
  useAutomationResults,
  useImportAutomationResults,
  useOperators,
  useWarehouseList,
} from "@/lib/queries";
import type { AuthStatus, BatchDetailResponse, BatchListResponse, GroupFacet, Product } from "@/lib/types";
import { useSessionStore, useWarehouseStore } from "@/features/stores";
import { Pagination } from "@/features/shared";
import { PageContent, PageHeader } from "@/components/layout";
import {
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
  Tabs,
  tabId,
  tabPanelId,
  toast,
} from "@/components/ui";
import { CategoryBar } from "@/features/warehouse/CategoryBar";
import { ProductCard } from "@/features/warehouse/ProductCard";
import { BatchCategoryDialog } from "@/features/warehouse/BatchCategoryDialog";
import { CreateBatchDialog } from "@/features/warehouse/CreateBatchDialog";
import { ProductDetailDrawer } from "@/features/warehouse/ProductDetailDrawer";
import { PricePreviewDialog } from "@/features/warehouse/PricePreviewDialog";
import { TransformDialog } from "@/features/warehouse/TransformDialog";
import { PublishDialog } from "@/features/publish/PublishDialog";
import {
  partitionMemberSelection,
  type QualityReviewSnapshot,
} from "@/features/warehouse/warehouseSelection";
import { ClaimJobsPanel } from "@/features/warehouse/ClaimJobsPanel";
import { AccountSwitcher } from "@/features/auth/UnlockPage";

const PAGE_SIZE_OPTIONS = [20, 50, 100, 200, 500, 1000];

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
  const tab: WarehouseTab = (tabValue === "batch" || (!isMember && tabValue === "automation"))
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
  const { clearSelected, deselectMany, selectMany, toggleSelected } = query;
  const queryClient = useQueryClient();
  const currentShopId = useSessionStore((state) => state.currentShopId);
  const { data: auth } = useAuthStatus();
  const connected = auth?.connected ?? false;
  const accountId = auth?.accountId ?? null;
  const operatorId = identity?.id ?? null;
  // Keep tracking a submitted task when the user returns to the product list.
  const claimJobs = useQuery({
    queryKey: queryKeys.claimJobs.list(operatorId, accountId),
    queryFn: () => api.get<BatchListResponse>("/claim-jobs", { query: { accountId } }),
    enabled: Boolean(accountId),
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
    refetchInterval: (state) => state.state.data?.items.some(
      (job) => job.status === "pending" || job.status === "running"
    ) ? 3000 : false,
  });
  const claimProgress = claimJobs.data?.items.map(
    (job) => `${job.id}:${job.currentIndex}:${job.status}`
  ).join("|");
  useEffect(() => {
    if (!claimProgress) return;
    void queryClient.invalidateQueries({ queryKey: queryKeys.warehouse.all });
    void queryClient.invalidateQueries({ queryKey: queryKeys.collectBox.all });
  }, [claimProgress, queryClient]);
  const operatorsQuery = useOperators({
    enabled: !isMember && (tab === "automation" || tab === "products"),
  });
  const isTeacher = !isMember && (identity?.role
    ? ["teacher", "admin"].includes(identity.role)
    : ["teacher", "admin"].includes(operatorsQuery.data?.current?.role ?? ""));
  const automationPage = Number(searchParams.get("automationPage") || "1");
  const automationQuery = useAutomationResults(automationPage, tab === "automation" && isTeacher);
  const importAutomationResults = useImportAutomationResults();
  const [automationSelected, setAutomationSelected] = useState<string[]>([]);
  // 入库时选择归到成品仓库的哪个分组（分类）。空串 = 不分组。
  const [importGroupId, setImportGroupId] = useState<string>("");
  // 「加入成品仓库」先弹框选分类，确认后才真正入库
  // （负责人口径 2026-11-11：加入的时候是选择，选完分组再加入）。
  const [importDialogOpen, setImportDialogOpen] = useState(false);

  const groupsQuery = useGroups();
  const reorderGroup = useReorderGroup();
  const createGroup = useCreateGroup();
  const deleteGroup = useDeleteGroup();
  const batchDelete = useBatchDelete();

  const category = query.category;
  // 搜索框已按界面精简要求移除；q 不再来自本页，避免旧状态残留影响列表
  const listQuery = useWarehouseList({
    page: query.page,
    pageSize: query.pageSize,
    q: null,
    accountId: auth?.accountId ?? null,
    claimAccountId: auth?.accountId ?? null,
    claimed: query.claimed,
    status: query.status,
    qualityStatus: isTeacher ? query.qualityStatus : null,
    sort: query.sort,
    cid: category.kind === "cid" ? category.cid : null,
    categoryId: category.kind === "categoryId" ? category.categoryId : null,
    groupId: category.kind === "group" ? category.groupId : null,
  });

  const data = listQuery.data;
  const items = useMemo(() => data?.items ?? [], [data?.items]);
  const facets = data?.facets;
  const total = data?.total ?? 0;
  // 总页数：工具栏里的「第 X / Y 页」与翻页按钮的禁用判断都用它。
  const pageCount = Math.max(1, Math.ceil(total / Math.max(1, query.pageSize)));
  const hasActiveWarehouseFilter = query.claimed !== null || query.status !== null
    || query.qualityStatus !== null || category.kind !== "all";

  const clearWarehouseFilters = (): void => {
    query.setClaimed(null);
    query.setStatus(null);
    query.setQualityStatus(null);
    query.setCategory({ kind: "all" });
  };

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
  // 选中时保存来源店铺，翻页后也能向发布弹窗提供完整的跨店判断数据。
  const [sourceShopIds, setSourceShopIds] = useState<Record<string, string>>({});
  // 学员的认领/发布动作跨页时也需要知道所选商品当前的认领状态。
  const [selectedClaimed, setSelectedClaimed] = useState<Record<string, boolean>>({});
  const [selectedPublishedShopIds, setSelectedPublishedShopIds] = useState<Record<string, string[]>>({});
  const [, setSelectedDetailIds] = useState<Record<string, string>>({});
  const [, setSelectedQualityReviewById] =
    useState<Record<string, QualityReviewSnapshot>>({});

  const claimOnly = useMutation({
    mutationFn: (body: { items: ReturnType<typeof toPublishItems>; shopId: string; accountId: string; claimOnly: boolean; confirm: boolean }) =>
      api.post<BatchDetailResponse>("/claim-jobs", body),
  });
  // 推送到妙手公共采集箱（把成品放进公共货源池，其他学员可认领）。
  // 与「认领到采集箱」方向相反：那是从公共箱复制到店铺采集箱。
  const pushCommonBox = useMutation({
    mutationFn: (body: { items: ReturnType<typeof toPublishItems>; accountId: string }) =>
      api.post<{ ok: number; fail: number; results: Array<{ productId: string; status: string; detailId?: string; reason?: string }> }>(
        "/common-box/push", body),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      if (result.fail > 0) {
        const first = result.results.find((r) => r.status !== "ok");
        toast.warning(`已推送 ${result.ok} 件，失败 ${result.fail} 件`, {
          description: first?.reason || "部分商品推送失败，可单独重试",
        });
      } else {
        toast.success(`已推送 ${result.ok} 件到公共采集箱`);
      }
    },
    onError: (error) => toast.error("推送公共采集箱失败", { description: apiErrorMessage(error) }),
  });
  const reconcileClaims = useMutation({
    mutationFn: () => api.post<{ cleared: number }>(
      `/warehouse/reconcile-claims?accountId=${encodeURIComponent(accountId ?? "")}`,
    ),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.warehouse.all });
      if (result.cleared > 0) {
        toast.info(`已按妙手采集箱状态修正 ${result.cleared} 件商品的认领标记`);
      }
    },
    onError: (error) => {
      toast.warning("无法核对当前账号的采集箱状态", {
        description: `${apiErrorMessage(error)}。原有认领标记已保留。`,
      });
    },
  });
  const reconcileMemberClaims = reconcileClaims.mutate;
  useEffect(() => {
    if (isMember && accountId) reconcileMemberClaims();
  }, [accountId, isMember, reconcileMemberClaims]);
  const selectedIds = query.selected;
  const selectedCount = selectedIds.length;
  const pageIds = items.map((item) => item.id);
  const { claimable: memberClaimableIds, publishable: memberPublishableIds } =
    partitionMemberSelection(selectedIds, selectedClaimed, selectedPublishedShopIds, currentShopId);
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

  const clearWarehouseSelection = useCallback(() => {
    clearSelected();
    setSourceShopIds({});
    setSelectedClaimed({});
    setSelectedPublishedShopIds({});
    setSelectedDetailIds({});
    setSelectedQualityReviewById({});
  }, [clearSelected]);

  useEffect(() => {
    clearWarehouseSelection();
  }, [auth?.accountId, clearWarehouseSelection]);

  const toggleWarehouseSelection = useCallback((id: string) => {
    const item = items.find((candidate) => candidate.id === id);
    if (selectedIds.includes(id)) {
      setSourceShopIds((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
      setSelectedClaimed((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
      setSelectedPublishedShopIds((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
      setSelectedDetailIds((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
      setSelectedQualityReviewById((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
    } else if (item) {
      setSourceShopIds((current) => ({ ...current, [id]: String(item.shopId ?? "") }));
      setSelectedClaimed((current) => ({ ...current, [id]: item.claimed }));
      setSelectedPublishedShopIds((current) => ({ ...current, [id]: item.publishedShopIds ?? [] }));
      setSelectedDetailIds((current) => ({ ...current, [id]: String(item.detailId ?? "") }));
      setSelectedQualityReviewById((current) => ({
        ...current,
        [id]: { status: item.qualityReviewStatus, reasons: item.qualityReviewReasons ?? [] },
      }));
    }
    toggleSelected(id);
  }, [items, selectedIds, toggleSelected]);

  const selectWarehouseItem = useCallback((id: string) => {
    const item = items.find((candidate) => candidate.id === id);
    if (item) setSourceShopIds((current) => ({ ...current, [id]: String(item.shopId ?? "") }));
    if (item) setSelectedClaimed((current) => ({ ...current, [id]: item.claimed }));
    if (item) setSelectedPublishedShopIds((current) => ({ ...current, [id]: item.publishedShopIds ?? [] }));
    if (item) setSelectedDetailIds((current) => ({ ...current, [id]: String(item.detailId ?? "") }));
    if (item) setSelectedQualityReviewById((current) => ({
      ...current,
      [id]: { status: item.qualityReviewStatus, reasons: item.qualityReviewReasons ?? [] },
    }));
    toggleSelected(id);
  }, [items, toggleSelected]);

  const selectOrDeselectPage = useCallback(() => {
    if (allSelectedOnPage) {
      deselectMany(pageIds);
      setSourceShopIds((current) => {
        const next = { ...current };
        for (const id of pageIds) delete next[id];
        return next;
      });
      setSelectedClaimed((current) => {
        const next = { ...current };
        for (const id of pageIds) delete next[id];
        return next;
      });
      setSelectedPublishedShopIds((current) => {
        const next = { ...current };
        for (const id of pageIds) delete next[id];
        return next;
      });
      setSelectedDetailIds((current) => {
        const next = { ...current };
        for (const id of pageIds) delete next[id];
        return next;
      });
      setSelectedQualityReviewById((current) => {
        const next = { ...current };
        for (const id of pageIds) delete next[id];
        return next;
      });
      return;
    }
    selectMany(pageIds);
    setSourceShopIds((current) => {
      const next = { ...current };
      for (const item of items) next[item.id] = String(item.shopId ?? "");
      return next;
    });
    setSelectedClaimed((current) => {
      const next = { ...current };
      for (const item of items) next[item.id] = item.claimed;
      return next;
    });
    setSelectedPublishedShopIds((current) => {
      const next = { ...current };
      for (const item of items) next[item.id] = item.publishedShopIds ?? [];
      return next;
    });
    setSelectedDetailIds((current) => {
      const next = { ...current };
      for (const item of items) next[item.id] = String(item.detailId ?? "");
      return next;
    });
    setSelectedQualityReviewById((current) => {
      const next = { ...current };
      for (const item of items) {
        next[item.id] = {
          status: item.qualityReviewStatus,
          reasons: item.qualityReviewReasons ?? [],
        };
      }
      return next;
    });
  }, [allSelectedOnPage, deselectMany, items, pageIds, selectMany]);

  // Store filter/page-size changes clear its selection internally; keep the local snapshots in sync.
  useEffect(() => {
    const selected = new Set(selectedIds);
    setSourceShopIds((current) => {
      const next = Object.fromEntries(Object.entries(current).filter(([id]) => selected.has(id)));
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
    setSelectedClaimed((current) => {
      const next = Object.fromEntries(Object.entries(current).filter(([id]) => selected.has(id)));
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
    setSelectedPublishedShopIds((current) => {
      const next = Object.fromEntries(Object.entries(current).filter(([id]) => selected.has(id)));
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
    setSelectedDetailIds((current) => {
      const next = Object.fromEntries(Object.entries(current).filter(([id]) => selected.has(id)));
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
    setSelectedQualityReviewById((current) => {
      const next = Object.fromEntries(Object.entries(current).filter(([id]) => selected.has(id)));
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
  }, [selectedIds]);

  const claimSelected = (ids: string[]): void => {
    if (!connected) {
      toast.warning("认领需要先连接妙手账号");
      navigate("/unlock", { state: { from: "/warehouse" } });
      return;
    }
    if (!currentShopId || !accountId) {
      toast.warning("请先选择要认领到的店铺");
      return;
    }
    if (claimOnly.isPending || ids.length === 0) return;
    const sessionEpoch = getOperatorSessionEpoch();
    claimOnly.mutate({
      items: toPublishItems(ids.map((id) => ({ warehouseId: id }))),
      shopId: currentShopId,
      accountId,
      claimOnly: true,
      confirm: true,
    }, {
      onSuccess: (result) => runIfOperatorSessionCurrent(sessionEpoch, () => {
        const listKey = queryKeys.claimJobs.list(operatorId, accountId);
        queryClient.setQueryData<BatchListResponse>(listKey, (previous) => {
          const previousItems = previous?.items ?? [];
          const exists = previousItems.some((job) => job.id === result.batch.id);
          return {
            items: [result.batch, ...previousItems.filter((job) => job.id !== result.batch.id)],
            total: (previous?.total ?? 0) + (exists ? 0 : 1),
          };
        });
        queryClient.setQueryData(queryKeys.claimJobs.detail(operatorId, accountId, result.batch.id), result);
        void queryClient.invalidateQueries({ queryKey: listKey, exact: true });
        // A response from an account switched away from belongs only to its cache.
        if (queryClient.getQueryData<AuthStatus>(queryKeys.auth.status())?.accountId !== accountId) return;
        clearWarehouseSelection();
        const next = new URLSearchParams(searchParams);
        next.set("tab", "batch");
        next.set("claimJob", result.batch.id);
        setSearchParams(next, { replace: true });
        toast.success(`已创建 ${ids.length} 件商品的认领任务`, {
          description: "只有成功导入采集箱的商品才会显示已认领。",
        });
      }),
      onError: (error) => runIfOperatorSessionCurrent(sessionEpoch, () => {
        toast.error("认领任务创建失败", { description: apiErrorMessage(error) });
      }),
    });
  };

  useEffect(() => {
    void queryClient.removeQueries({ queryKey: ["warehouse"] });
    void queryClient.removeQueries({ queryKey: ["system", "templates"] });
    clearWarehouseSelection();
  }, [identity?.id, identity?.role, clearWarehouseSelection, queryClient]);

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
        clearWarehouseSelection();
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
    if (accountId) void claimJobs.refetch();
  };

  const headerActions = (
    <>
      <AccountSwitcher />
      <IconButton
        label="刷新"
        size="md"
        variant="secondary"
        icon={<RefreshCw className={cn(listQuery.isFetching && "animate-spin")} />}
        onClick={refreshAll}
      />
    </>
  );

  return (
    <>
      <PageHeader
        title="成品仓库"
        actions={headerActions}
      />

      <PageContent width="table">
        <Tabs
          value={tab}
          onChange={(value) => setTab(value as WarehouseTab)}
          idBase="warehouse"
          items={[
            { value: "products", label: "商品列表", icon: <PackageSearch className="size-3.5" /> },
            ...(!isMember ? [
              { value: "automation", label: "自动化结果", icon: <Zap className="size-3.5" /> },
            ] as const : []),
            { value: "batch", label: "批次", icon: <ListChecks className="size-3.5" /> },
          ]}
        />

        {tab === "batch" && (
          <div
            role="tabpanel"
            id={tabPanelId("warehouse", "batch")}
            aria-labelledby={tabId("warehouse", "batch")}
          >
            <ClaimJobsPanel
              key={`${operatorId}:${accountId}`}
              accountId={accountId}
              operatorId={operatorId}
              list={claimJobs}
              focusJobId={searchParams.get("claimJob")}
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
              <Card><EmptyState title="暂无待入仓的自动化结果" description="自动化处理成功的商品会保存在这里，由老师选择后加入成品仓库。" minHeight={220} /></Card>
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
                {/* 卡片网格：与成品仓库完全一致（同一个 ProductCard）。
                    负责人口径 2026-11-11：自动化结果也要像成品仓库那样
                    卡片显示名字 / 价格 / 类目，而不是一行文字。 */}
                <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
                  {automationQuery.data.items.map((item, index) => (
                    <ProductCard
                      key={item.id}
                      product={item}
                      index={index}
                      // 自动化结果还没人工编辑过，「谁编辑于何时」没有意义
                      showEditor={false}
                      selected={automationSelected.includes(item.id)}
                      onToggle={(id) => setAutomationSelected((selected) => selected.includes(id)
                        ? selected.filter((value) => value !== id)
                        : [...selected, id])}
                      onOpen={() => navigate(`/warehouse/${item.id}`)}
                    />
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
            <CategoryBar
              categories={facets?.categories ?? []}
              groups={facets?.groups ?? groupsQuery.data?.groups.map((group) => ({
                id: group.id,
                name: group.name,
                color: group.color,
                count: group.count,
              })) ?? []}
              active={category}
              total={facets?.total ?? total}
              loading={listQuery.isLoading}
              manageable={!isMember}
              onSelect={(filter) => query.setCategory(filter)}
              onCreateGroup={() => {
                setGroupName("");
                setGroupError(null);
                setCreateOpen(true);
              }}
              onDeleteGroup={(group) => setDeleteGroupTarget(group)}
              // 拖拽排序：按新顺序把 sort 依次写成 1..N（老师/管理员才有拖拽手柄）。
              // 一串 PATCH 打过去而不是新开批量接口：分组就十来个，且
              // group_service.update 已经处理了 sort 校验。
              onReorder={(orderedIds) => {
                orderedIds.forEach((gid, index) => {
                  reorderGroup.mutate({ id: gid, sort: index + 1 });
                });
                void groupsQuery.refetch();
              }}
            />

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
              {isTeacher && (
                <Select
                  size="md"
                  aria-label="规格核验状态"
                  className="w-[148px]"
                  value={query.qualityStatus ?? "all"}
                  onChange={(value) => query.setQualityStatus(value === "all" ? null : value as "verified" | "needs_review")}
                  options={[
                    { value: "all", label: "全部规格" },
                    { value: "needs_review", label: "待核验规格" },
                    { value: "verified", label: "已核验规格" },
                  ]}
                />
              )}

              {!listQuery.isLoading && !listQuery.isError && items.length > 0 && (
                <span className="num ml-1 flex shrink-0 items-center gap-1.5 text-xs text-fg-muted">
                  本页 {formatCount(items.length)} 件
                  {listQuery.isFetching && (
                    <RefreshCw className="size-3 animate-spin" aria-label="正在刷新" />
                  )}
                </span>
              )}

              {/* 每页条数 + 翻页 + 全选全部并到这一行：底部不再重复一套分页器，
                  用户不用滑到列表底部才能翻页。 */}
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

                <div className="flex shrink-0 items-center gap-1">
                  <span className="num px-1 text-xs text-fg-muted">
                    第 {query.page} / {pageCount} 页
                  </span>
                  <Button
                    variant="secondary"
                    size="md"
                    className="size-9 shrink-0 p-0"
                    disabled={query.page <= 1 || listQuery.isFetching}
                    aria-label="上一页"
                    title="上一页"
                    onClick={() => {
                      if (isMember) clearWarehouseSelection();
                      query.setPage(Math.max(1, query.page - 1));
                    }}
                  >
                    <ChevronLeft className="size-4" />
                  </Button>
                  <Button
                    variant="secondary"
                    size="md"
                    className="size-9 shrink-0 p-0"
                    disabled={query.page >= pageCount || listQuery.isFetching}
                    aria-label="下一页"
                    title="下一页"
                    onClick={() => {
                      if (isMember) clearWarehouseSelection();
                      query.setPage(Math.min(pageCount, query.page + 1));
                    }}
                  >
                    <ChevronRight className="size-4" />
                  </Button>
                </div>

                <Button
                  variant="secondary"
                  size="md"
                  icon={<CheckSquare />}
                  className="shrink-0"
                  onClick={selectOrDeselectPage}
                  disabled={pageIds.length === 0}
                  title={allSelectedOnPage ? "取消本页全部选中" : "选中本页全部商品"}
                >
                  {allSelectedOnPage ? "取消全选" : "全选"}
                  {selectedCount > 0 && <span className="num text-fg-muted">（{selectedCount}）</span>}
                </Button>
                {/* 设置分类 / 删除放到全选旁边（负责人口径 2026-11-11）：
                    这两个是最高频的整理动作，每次都要滚到底部操作条去点太绕。
                    底部操作条因此只剩「认领 / 推送 / 发布」这类流程动作。 */}
                <Button
                  variant="secondary"
                  size="md"
                  icon={<Layers />}
                  className="shrink-0"
                  onClick={() => setBatchOpen(true)}
                  disabled={selectedCount === 0}
                  title="给选中商品设置分类"
                >
                  设置分类
                </Button>
                <Button
                  variant="secondary"
                  size="md"
                  icon={<Trash2 />}
                  className="shrink-0"
                  onClick={() => setDeleteOpen(true)}
                  disabled={selectedCount === 0}
                  title="删除选中商品"
                >
                  删除
                </Button>
              </div>
            </div>

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
                  title={hasActiveWarehouseFilter ? "没有符合条件的商品" : "成品仓库暂无商品"}
                  description={hasActiveWarehouseFilter
                    ? "当前筛选没有匹配商品，可以清除筛选后重新查看。"
                    : isMember
                      ? "共享成品由老师核对后加入；入仓后即可认领并按自己的店铺改价发布。"
                      : "自动化处理成功的商品不会自动入仓，请在自动化结果中核对后选择加入。"}
                  action={hasActiveWarehouseFilter
                    ? { label: "清除筛选", onClick: clearWarehouseFilters }
                    : isTeacher
                      ? { label: "查看自动化结果", onClick: () => setTab("automation") }
                      : undefined}
                />
              </Card>
            )}

            {!listQuery.isLoading && !listQuery.isError && items.length > 0 && (
              <>
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
                          onToggle={toggleWarehouseSelection}
                          onOpen={(item) => navigate(`/warehouse/${item.id}`)}
                        />
                      </motion.div>
                    ))}
                  </AnimatePresence>
                </motion.div>
              </>
            )}

            {/* 分页器已并入上方工具栏（每页条数 + 页码 + 翻页 + 全选同排），
                这里不再重复一套，列表底部保持干净。 */}
          </div>
        )}
      </PageContent>

      {/* 浮动批量操作条（design-system §5.8）：只在商品列表 tab 出现，
          看批次时还飘着「已选 12 件」会让人以为选的是批次 */}
      <BulkActionBar
        count={tab === "automation" ? automationSelected.length : tab === "products" ? selectedCount : 0}
        total={tab === "automation" ? automationQuery.data?.total : total}
        unit="件"
        onClear={() => tab === "automation" ? setAutomationSelected([]) : clearWarehouseSelection()}
        actions={tab === "automation" ? [{
            label: importAutomationResults.isPending ? "正在加入…" : "加入成品仓库",
            icon: <PackageCheck className="size-3.5" />,
            disabled: importAutomationResults.isPending,
            onClick: () => setImportDialogOpen(true),
          }] : isMember ? [
          ...(memberClaimableIds.length > 0 ? [{
            label: claimOnly.isPending
              ? "正在认领…"
              : `认领 ${memberClaimableIds.length} 件`,
            icon: <PackageCheck className="size-3.5" />,
            disabled: claimOnly.isPending,
            onClick: () => claimSelected(memberClaimableIds),
          }] : []),
          ...(selectedIds.length > 0 && !isMember ? [{
            label: pushCommonBox.isPending
              ? "正在推送…"
              : `导入公共采集箱 ${selectedIds.length} 件`,
            icon: <Globe className="size-3.5" />,
            disabled: pushCommonBox.isPending || !accountId,
            onClick: () => {
              if (!accountId) {
                toast.warning("推送公共采集箱需要先连接妙手账号");
                return;
              }
              pushCommonBox.mutate({
                items: toPublishItems(selectedIds.map((id) => ({ warehouseId: id }))),
                accountId,
              });
            },
          }] : []),
          ...(selectedIds.length > 0 ? [{
            // 认领并发布：复制载体 → 覆盖数据 → 直接发布到店铺（一步完成）
            label: `认领并发布 ${selectedIds.length} 件`,
            icon: <Rocket className="size-3.5" />,
            onClick: () => openPublish(selectedIds),
          }] : []),
          ...(memberPublishableIds.length > 0 ? [{
            label: "发布已认领商品",
            icon: <Send className="size-3.5" />,
            onClick: () => openPublish(memberPublishableIds),
          }] : []),
        ] : [
          {
            label: claimOnly.isPending ? "正在认领…" : "认领到采集箱",
            icon: <Inbox className="size-3.5" />,
            disabled: claimOnly.isPending,
            onClick: () => claimSelected(selectedIds),
          },
          {
            label: pushCommonBox.isPending ? "正在推送…" : "导入公共采集箱",
            icon: <Globe className="size-3.5" />,
            disabled: pushCommonBox.isPending || selectedIds.length === 0,
            onClick: () => {
              if (!accountId) {
                toast.warning("推送公共采集箱需要先连接妙手账号");
                return;
              }
              pushCommonBox.mutate({
                items: toPublishItems(selectedIds.map((id) => ({ warehouseId: id }))),
                accountId,
              });
            },
          },

          {
            label: "发布",
            icon: <Send className="size-3.5" />,
            onClick: () => openPublish(selectedIds),
          },
        ]}
      />

      {/* 加入成品仓库：先选分类，确认后才入库 */}
      <Dialog
        open={importDialogOpen}
        onOpenChange={(open) => {
          setImportDialogOpen(open);
          if (!open) setImportGroupId("");
        }}
        size="sm"
        title="加入成品仓库"
        description={`将选中的 ${automationSelected.length} 件商品加入成品仓库。`}
        closeOnOverlay={false}
        footer={
          <>
            <Button variant="secondary" onClick={() => setImportDialogOpen(false)}>
              取消
            </Button>
            <Button
              variant="primary"
              loading={importAutomationResults.isPending}
              onClick={() => {
                importAutomationResults.mutate(
                  { ids: automationSelected, groupId: importGroupId || null },
                  {
                    onSuccess: (result) => {
                      setAutomationSelected([]);
                      setImportDialogOpen(false);
                      setImportGroupId("");
                      toast.success(`已加入成品仓库 ${formatCount(result.added)} 件`);
                    },
                    onError: (error) =>
                      toast.error("加入成品仓库失败", { description: apiErrorMessage(error) }),
                  },
                );
              }}
            >
              确认加入
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-2">
          <label className="text-xs text-fg-muted" htmlFor="import-group">
            入库分类
          </label>
          <select
            id="import-group"
            className="h-9 rounded-sm border border-border-subtle bg-canvas px-2 text-sm text-fg"
            value={importGroupId}
            onChange={(event) => setImportGroupId(event.target.value)}
          >
            <option value="">不分组</option>
            {(groupsQuery.data?.groups ?? []).map((group) => (
              <option key={group.id} value={String(group.id)}>
                {group.name}（{formatCount(group.count)} 件）
              </option>
            ))}
          </select>
          <p className="text-2xs text-fg-muted">
            选中的商品入库后会归到这个分类；不分组时可在成品仓库里再调整。
          </p>
        </div>
      </Dialog>

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
          clearWarehouseSelection();
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
        accountId={accountId ?? auth?.accountId ?? null}
        connected={connected}
        revisions={hasSelectedRevisions ? selectedRevisions : undefined}
        onRefresh={() => void listQuery.refetch()}
        onDone={clearWarehouseSelection}
      />}

      {!isMember && <CreateBatchDialog
        open={createBatchOpen}
        onOpenChange={setCreateBatchOpen}
        ids={selectedIds}
        products={items}
        accountId={auth?.accountId ?? null}
        onCreated={() => {
          clearWarehouseSelection();
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
          clearWarehouseSelection();
          selectWarehouseItem(id);
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
        onSubmitted={clearWarehouseSelection}
      />
    </>
  );
}

export default WarehousePage;
