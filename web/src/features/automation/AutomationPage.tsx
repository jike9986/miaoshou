// features/automation —— 自动化上品（老版第一个板块，一屏布局重做）
//
// 版面（对齐老版 0.1.4，一屏内全部可见）：
//   ┌──────────────────────────────────────┬──────────────┐
//   │ 选择要处理的商品（单行工具栏 + 卡片网格）│ 处理状态      │
//   │ 处理设置（语言/四开关同行/七格数字/尺码图）│ 任务摘要      │
//   └──────────────────────────────────────┴──────────────┘
//   顶部工具条：账号切换 · 店铺切换 · 开始处理（右侧固定 320px 进度列）
//
// 连接妙手：左侧导航已有独立板块，本页不再重复放连接面板（老版也只是个折叠条）。
//
// 真实接口（全部复用 @/lib/queries + @/lib/api，不臆造）：
//   GET   /auth/status                     useAuthStatus（连接面板 + 连接态）
//   POST  /auth/connect                    useConnectAccount（连接 / 切账号）
//   PATCH /auth/accounts/{accountId}       改名（无 hook，直接 api.patch）
//   GET   /collect-box                     useCollectBoxList（选品列表，扫描即查询）
//   GET   /system/templates                useTemplates（定价模板名）
//   GET   /size-charts                     useSizeCharts（尺码图库）
//   DELETE/size-charts/{id}                useDeleteSizeChart
//   POST  /collect-box/{detailId}/process  useProcessCollectItem（真正跑一件）
//
// 明确不可用的能力（按钮禁用并给出 title 说明，不假装）：
//   · 上传尺码图 —— 后端只有「登记图床地址」的 POST /size-charts，没有文件上传接口
//   · 全结果批量选择 —— 后端 /collect-box 不支持一次返回全量选品，需逐页选择

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Pencil, Play, RefreshCw, Square, Tag } from "lucide-react";
import { apiErrorMessage, isApiError } from "@/lib/api";
import { formatCount, cn } from "@/lib/utils";
import { queryKeys } from "@/lib/queryKeys";
import { useIsNarrow, useMediaQuery } from "@/hooks/useMediaQuery";
import {
  useAuthStatus,
  useCollectBoxList,
  useProcessCollectItem,
  usePublish,
  useShops,
  useTemplates,
  useWorkflowDefaults,
  useSaveWorkflowDefaults,
  useCreateAutomationRun,
  useLatestAutomationRun,
  useRecoverAutomationRun,
  useStartAutomationRunItem,
  useAutomationRunProcessResult,
  useStartAutomationPublish,
  useResumeAutomationPublish,
  useRetryAutomationPublish,
  useAutomationPublishResult,
  useRefreshPublishLogs,
  useProductDetail,
} from "@/lib/queries";
import { useSizeCharts, pickCharts, pickDefaultUrl, buildDefaultSizeChart } from "@/features/automation/useSizeCharts";
import { PriceTemplateDialog } from "@/features/warehouse/PriceTemplateDialog";
import type { AutomationPublishConfirmation, AutomationRunRecord, CollectBoxItem } from "@/lib/types";
import { PageContent, PageHeader } from "@/components/layout";
import {
  Badge,
  Button,
  Card,
  CardTitle,
  Dialog,
  EmptyState,
  InlineError,
  SkeletonRows,
  toast,
} from "@/components/ui";
import { ProcessSettings } from "@/features/automation/ProcessSettings";
import { pendingAutomationPublishAction } from "@/features/automation/automationPublish";
import { ProductPicker } from "@/features/automation/ProductPicker";
import { ProgressPanel } from "@/features/automation/ProgressPanel";
import { AccountSwitcher } from "@/features/auth/UnlockPage";
import { useSessionStore } from "@/features/stores";
import {
  DEFAULT_SETTINGS,
  IDLE_RUN,
  loadSettings,
  saveSettings,
  type AutomationSettings,
  type ItemStage,
  type ItemProgress,
  type ListFilter,
  type RunFailure,
  type RunState,
} from "@/features/automation/types";

/** 已勾选商品的快照：跨页勾选后仍能拿到 detailId / shopId 去处理 */
interface SelectionEntry {
  key: string;
  detailId: string;
  shopId: string;
  title: string;
}

const MAX_RUN_ITEMS = 200;

const QUALITY_REASON_LABELS: Record<string, string> = {
  missing_weight: "缺少重量",
  missing_dimensions: "缺少包裹尺寸",
  default_dimensions: "包裹尺寸仍是默认值",
  unverified: "尚未人工核验",
  stale_review: "核验记录对应旧版本",
};

function measureLabel(value: unknown, unit: string): string {
  if (value === null || value === undefined || value === "") return "未填写";
  return `${String(value)} ${unit}`;
}

const DATE_FORMAT = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

export function AutomationPage() {
  const queryClient = useQueryClient();
  const { data: status } = useAuthStatus();
  const connected = status?.connected ?? false;
  const currentShopId = useSessionStore((state) => state.currentShopId);
  const narrow = useIsNarrow();
  const shortViewport = useMediaQuery("(max-height: 760px)");
  const scrollLayout = narrow || shortViewport;

  /* ---------------- 列表查询参数 ---------------- */
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [filter, setFilter] = useState<ListFilter>("all");

  const listQuery = useCollectBoxList(
    // status 必须传 "notPublished"：后端把 "all" 翻译成 upstream_status=None，
    // 等于让妙手不做发布状态过滤，已发布的老商品会被一起扫进来。
    // 用户打开妙手采集箱看到的是「未发布」那一条，两边必须对齐，
    // 否则就会出现「页面 20 条、实际 1 条」的错位。
    // 页面上的「全部/已勾选/待处理…」是本地筛选（ListFilter），不参与这个参数。
    { page, pageSize, status: "notPublished", accountId: status?.accountId ?? null },
    connected
  );

  const items = useMemo<CollectBoxItem[]>(() => listQuery.data?.items ?? [], [listQuery.data?.items]);
  const total = listQuery.data?.total ?? 0;
  const hasMore = listQuery.data?.hasMore ?? false;

  const refetchList = listQuery.refetch;

  /* ---------------- 处理设置 ---------------- */
  // 惰性初始化：先读存档，读不到才用默认值（老版 persistTaskPreferences 的等价物）
  const [settings, setSettings] = useState<AutomationSettings>(loadSettings);
  const patchSettings = useCallback((patch: Partial<AutomationSettings>) => {
    setSettings((prev) => ({ ...prev, ...patch }));
  }, []);

  /* ---------- 工作流默认值同步（重量/包裹尺寸/尺码图 → 后端） ----------
     后端 save_from_collect_box 读的是 workflow.defaults，不是这里的 localStorage。
     改动这三个值时必须同步过去，否则「界面上调了参数，处理时用的还是旧值」。 */
  const workflowQuery = useWorkflowDefaults(connected);
  const saveWorkflow = useSaveWorkflowDefaults();
  const sizeChartsQuery = useSizeCharts(connected);
  const chartList = useMemo(() => pickCharts(sizeChartsQuery.data), [sizeChartsQuery.data]);
  const defaultChartUrl = pickDefaultUrl(sizeChartsQuery.data);
  const fallbackChart = buildDefaultSizeChart(defaultChartUrl);

  // 首次拿到后端值时回填本地显示（后端为准），之后本地改动再推上去
  const syncedFromServer = useRef(false);
  useEffect(() => {
    if (syncedFromServer.current || !workflowQuery.data?.workflowDefaults) return;
    const server = workflowQuery.data.workflowDefaults;
    const patch: Partial<AutomationSettings> = {};
    if (typeof server.weightIncrement === "number") patch.weightExtraKg = server.weightIncrement;
    if (server.packageDimensions) {
      patch.lengthCm = server.packageDimensions.packageLength;
      patch.widthCm = server.packageDimensions.packageWidth;
      patch.heightCm = server.packageDimensions.packageHeight;
    }
    if (Object.keys(patch).length > 0) setSettings((prev) => ({ ...prev, ...patch }));
    syncedFromServer.current = true;
  }, [workflowQuery.data]);

  // 本地改动 → 推送到后端（防抖 800ms，避免拖动数字时连打）
  const pushTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!connected || !syncedFromServer.current) return;
    if (pushTimer.current) clearTimeout(pushTimer.current);
    pushTimer.current = setTimeout(() => {
      const selectedChart = chartList.find((c) => c.id === settings.sizeChartId);
      const chartUrl = selectedChart?.url ?? (settings.sizeChartId === null ? (fallbackChart?.url ?? "") : "");
      saveWorkflow.mutate({
        weightIncrement: settings.weightExtraKg,
        packageDimensions: {
          packageLength: settings.lengthCm,
          packageWidth: settings.widthCm,
          packageHeight: settings.heightCm,
        },
        sizeChartUrl: chartUrl,
      });
    }, 800);
    return () => { if (pushTimer.current) clearTimeout(pushTimer.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.weightExtraKg, settings.lengthCm, settings.widthCm, settings.heightCm, settings.sizeChartId, connected]);

  // 每次改动都落盘。放在 effect 里而不是 patchSettings 内：模板/尺码图可能
  // 由别处 setSettings 改，统一在这里兜住。
  useEffect(() => {
    saveSettings(settings);
  }, [settings]);

  const templatesQuery = useTemplates(connected);
  const templateNames = useMemo(
    () =>
      (templatesQuery.data?.templates ?? [])
        .filter((template) => !template.invalid)
        .map((template) => template.name),
    [templatesQuery.data?.templates]
  );
  const templatesError = templatesQuery.isError
    ? apiErrorMessage(templatesQuery.error)
    : null;

  /* ---------------- 勾选（跨页快照） ---------------- */
  const [selection, setSelection] = useState<Map<string, SelectionEntry>>(() => new Map());
  const selectedKeys = useMemo(() => new Set(selection.keys()), [selection]);

  const toggleItem = useCallback((item: CollectBoxItem) => {
    if (!selectedKeys.has(item.key) && selection.size >= MAX_RUN_ITEMS) {
      toast.warning(`一次最多选择 ${MAX_RUN_ITEMS} 件商品`);
      return;
    }
    setSelection((prev) => {
      const next = new Map(prev);
      if (next.has(item.key)) {
        next.delete(item.key);
      } else {
        next.set(item.key, {
          key: item.key,
          detailId: item.detailId,
          shopId: item.shopId,
          title: item.title,
        });
      }
      return next;
    });
  }, [selectedKeys, selection.size]);

  /* ---------------- 本地处理态 ---------------- */
  const [stages, setStages] = useState<Record<string, ItemStage>>({});
  // 每件商品完成了哪几步（老版 productStatus 的 textCompleted / imagesCompleted）
  const [progress, setProgress] = useState<Record<string, ItemProgress>>({});
  const [run, setRun] = useState<RunState>(IDLE_RUN);
  const createRunMutation = useCreateAutomationRun();
  // 查找该妙手账号最近的任务，不按当前店铺过滤；任务恢复后沿用它自己的目标店铺。
  const latestRunQuery = useLatestAutomationRun(status?.accountId ?? null, null, connected);
  const shopsQuery = useShops(status?.accountId, { enabled: connected });
  const recoverRunMutation = useRecoverAutomationRun();
  const startRunItemMutation = useStartAutomationRunItem();
  const recordProcessMutation = useAutomationRunProcessResult();
  const startPublishMutation = useStartAutomationPublish();
  const resumePublishMutation = useResumeAutomationPublish();
  const retryPublishMutation = useRetryAutomationPublish();
  const recordPublishMutation = useAutomationPublishResult();
  const refreshPublishLogsMutation = useRefreshPublishLogs();
  const persistentRunId = useRef<string | null>(null);
  const persistentItems = useRef<Map<string, string>>(new Map());
  const hydratedRunId = useRef<string | null>(null);
  const recoveringRunId = useRef<string | null>(null);
  const startingRunRef = useRef(false);
  const [preparingRun, setPreparingRun] = useState(false);
  const [specificationPrompt, setSpecificationPrompt] = useState<{
    productId: string;
    title: string;
    stale: boolean;
  } | null>(null);
  const specificationResolver = useRef<((value: AutomationPublishConfirmation | null) => void) | null>(null);
  const specificationQuery = useProductDetail(specificationPrompt?.productId ?? null, true);
  const running = run.phase === "running";
  const savedRun = latestRunQuery.data?.run;
  const targetShopId = savedRun?.shopId ?? currentShopId;
  const targetShopName = shopsQuery.data?.shops.find((shop) => shop.shopId === targetShopId)?.name;
  const publishShopName = savedRun
    ? targetShopName ?? "任务目标店铺"
    : targetShopName ?? (currentShopId ? "当前店铺" : "未选择店铺");
  // 两个开关都关着就没有可执行内容：按钮置灰并说明原因（老版 summary-warning）
  const somethingEnabled = settings.textEnabled || settings.imagesEnabled;
  const publishConfigIssue = settings.publishEnabled
    ? !settings.textEnabled
      ? "正式发布前请开启文字处理。"
      : !settings.imagesEnabled
        ? "正式发布前请开启图片处理。"
        : !settings.priceTemplateEnabled || !settings.template?.trim()
          ? "正式发布前请启用并选择定价模板。"
          : null
    : null;
  const stopRef = useRef(false);

  /* ---------------- 客户端筛选 ---------------- */
  const visibleItems = useMemo(() => {
    switch (filter) {
      case "selected":
        return items.filter((item) => selectedKeys.has(item.key));
      case "pending":
        return items.filter((item) => {
          const stage = stages[item.key];
          return stage === undefined || stage === "queued";
        });
      case "running":
        return items.filter((item) => stages[item.key] === "running");
      case "done":
        return items.filter((item) => stages[item.key] === "done");
      case "failed":
        return items.filter((item) => stages[item.key] === "failed");
      case "needs_review":
        return items.filter((item) => stages[item.key] === "needs_review");
      case "all":
      default:
        return items;
    }
  }, [filter, items, selectedKeys, stages]);

  const hydrateRun = useCallback((saved: AutomationRunRecord) => {
    if (hydratedRunId.current === saved.id) return;
    hydratedRunId.current = saved.id;
    persistentRunId.current = saved.id;
    persistentItems.current = new Map(saved.items.map((item) => [item.key, item.id]));
    const nextSelection = new Map(saved.items.filter((item) => item.processStatus === "queued"
      || pendingAutomationPublishAction(item) !== null).map((item) => [item.key, {
      key: item.key, detailId: item.detailId, shopId: item.shopId, title: item.title,
    }]));
    setSelection(nextSelection);
    const savedSettings = saved.settings;
    setSettings((current) => ({
      ...current,
      ...Object.fromEntries(Object.entries(savedSettings).filter(([key, value]) =>
        key in current && typeof value === typeof current[key as keyof AutomationSettings])),
    }));
    const nextStages: Record<string, ItemStage> = {};
    const nextProgress: Record<string, ItemProgress> = {};
    const failures: RunFailure[] = [];
    let done = 0;
    let failed = 0;
    let needsReview = 0;
    let imageDone = 0;
    let textDone = 0;
    for (const item of saved.items) {
      const stage: ItemStage = item.processStatus === "failed" ? "failed"
        : item.processStatus === "needs_review" || item.publishStatus === "needs_review" || item.publishStatus === "running" ? "needs_review"
          : item.processStatus === "running" ? "needs_review"
            : item.processStatus === "succeeded" ? "done"
              : "queued";
      nextStages[item.key] = stage;
      if (stage === "done" || stage === "failed" || stage === "needs_review") done += 1;
      if (stage === "failed") failed += 1;
      if (stage === "needs_review") needsReview += 1;
      const images = item.processResult.images;
      if (images) imageDone += images.submitted ?? 0;
      const textComplete = item.processResult.textDone === true;
      if (textComplete) textDone += 1;
      nextProgress[item.key] = {
        textDone: textComplete,
        imagesDone: Boolean(images && images.submitted > 0 && images.failed === 0),
        imageFailures: images?.failed ?? 0,
      };
      const reason = item.processReason || item.publishReason;
      if (stage === "failed" || stage === "needs_review") failures.push({
        key: item.key, title: item.title, detailId: item.detailId,
        reason: reason || (stage === "needs_review" ? "处理状态未知，请核对妙手记录后再继续。" : "处理失败"),
      });
    }
    const planned = saved.settings.imagesEnabled === true
      ? saved.items.length * (Number(saved.settings.mainImageCount ?? 0)
        + Number(saved.settings.skuImageCount ?? 0) + Number(saved.settings.detailImageCount ?? 0)) : 0;
    const isInFlight = saved.status === "running";
    setStages(nextStages);
    setProgress(nextProgress);
    setRun({
      phase: isInFlight || saved.status === "queued" ? "stopped" : "done",
      total: saved.items.length, done, failed, needsReview,
      startedAt: saved.createdAt * 1000,
      endedAt: saved.finishedAt ? saved.finishedAt * 1000 : Date.now(),
      imageDone, imagePlanned: planned, textDone,
      stage: needsReview ? `${needsReview} 项待核对` : "已恢复上次运行",
      failedKeys: failures.map((failure) => failure.key), failures,
    });
  }, []);

  useEffect(() => {
    const saved = latestRunQuery.data?.run;
    if (running || !saved || hydratedRunId.current === saved.id || recoveringRunId.current === saved.id) return;
    if (saved.status === "queued") {
      hydrateRun(saved);
      return;
    }
    recoveringRunId.current = saved.id;
    void (async () => {
      try {
        const recovered = (await recoverRunMutation.mutateAsync(saved.id)).run;
        hydrateRun(recovered);
      } catch {
        hydrateRun(saved);
      }
      recoveringRunId.current = null;
    })();
  }, [latestRunQuery.data?.run, recoverRunMutation, hydrateRun, running]);

  const selectVisible = useCallback(() => {
    const allSelected = visibleItems.length > 0 && visibleItems.every((item) => selection.has(item.key));
    const additions = allSelected ? 0 : visibleItems.filter((item) => !selection.has(item.key)).length;
    const available = Math.max(0, MAX_RUN_ITEMS - selection.size);
    if (additions > available) {
      toast.warning(`一次最多选择 ${MAX_RUN_ITEMS} 件商品`, {
        description: available > 0
          ? `已保留现有选择，并从本页补选 ${available} 件。`
          : "已保留现有选择，请取消部分商品后再继续。",
      });
    }
    setSelection((prev) => {
      const next = new Map(prev);
      const allSelected = visibleItems.length > 0 && visibleItems.every((item) => next.has(item.key));
      let remaining = Math.max(0, MAX_RUN_ITEMS - next.size);
      for (const item of visibleItems) {
        if (allSelected) {
          next.delete(item.key);
        } else if (!next.has(item.key) && remaining > 0) {
          next.set(item.key, {
            key: item.key,
            detailId: item.detailId,
            shopId: item.shopId,
            title: item.title,
          });
          remaining -= 1;
        }
      }
      return next;
    });
  }, [selection, visibleItems]);

  /* ---------------- 真正执行：串行处理入仓 ---------------- */
  const processItem = useProcessCollectItem();
  const publish = usePublish();
  // 新任务跟随顶部店铺，恢复任务继续使用创建时保存的目标店铺。

  const requestSpecificationConfirmation = useCallback((
    productId: string,
    title: string,
    stale: boolean,
  ): Promise<AutomationPublishConfirmation | null> => new Promise((resolve) => {
    if (specificationResolver.current) specificationResolver.current(null);
    specificationResolver.current = resolve;
    setSpecificationPrompt({ productId, title, stale });
    void queryClient.invalidateQueries({
      queryKey: queryKeys.warehouse.detail(productId, true),
      exact: true,
    });
  }), [queryClient]);

  const resolveSpecificationPrompt = (value: AutomationPublishConfirmation | null): void => {
    specificationResolver.current?.(value);
    specificationResolver.current = null;
    setSpecificationPrompt(null);
  };

  const startOrResumePublish = async (
    input: { runId: string; itemId: string; productId: string; title: string; resume: boolean },
  ): Promise<"started" | "skipped"> => {
    let confirmation: AutomationPublishConfirmation | undefined;
    while (true) {
      try {
        const request = {
          runId: input.runId,
          itemId: input.itemId,
          ...(confirmation ?? {}),
        };
        if (input.resume) await resumePublishMutation.mutateAsync(request);
        else await startPublishMutation.mutateAsync(request);
        return "started";
      } catch (error) {
        if (!isApiError(error) || error.code !== "QUALITY_REVIEW_REQUIRED") throw error;
        confirmation = await requestSpecificationConfirmation(
          input.productId,
          input.title,
          error.details?.confirmationStale === true,
        ) ?? undefined;
        if (!confirmation) return "skipped";
      }
    }
  };

  const setPublishQueued = (key: string): void => {
    setStages((prev) => ({ ...prev, [key]: "queued" }));
  };

  const resumePublishTargets = async (
    targets: SelectionEntry[], saved: AutomationRunRecord,
  ): Promise<void> => {
    const failures: RunFailure[] = [];
    let submitted = 0;
    setRun({
      phase: "running", total: targets.length, done: 0, failed: 0,
      startedAt: Date.now(), endedAt: null, imageDone: 0, imagePlanned: 0,
      stage: "检查图片并续发…", failures: [], failedKeys: [],
    });
    const retryableKeys = new Set<string>();
    for (const [index, target] of targets.entries()) {
      const item = saved.items.find((candidate) => candidate.key === target.key);
      if (!item) continue;
      const action = pendingAutomationPublishAction(item);
      setRun((prev) => ({
        ...prev,
        stage: `${action === "resume" ? "续发" : action === "retry" ? "重试发布" : "发布"}：${target.title || target.detailId}`,
      }));
      let resumed = false;
      try {
        if (action === "retry") {
          await retryPublishMutation.mutateAsync({ runId: saved.id, itemId: item.id });
        }
        const resume = action === "resume";
        const started = await startOrResumePublish({
          runId: saved.id,
          itemId: item.id,
          productId: String(item.productId ?? ""),
          title: target.title || item.title || target.detailId,
          resume,
        });
        if (started === "skipped") {
          retryableKeys.add(target.key);
          setPublishQueued(target.key);
          failures.push({
            key: target.key,
            title: target.title || item.title || "",
            detailId: target.detailId,
            reason: "已跳过本次发布；商品保留在待发布队列，可再次选择后继续。",
          });
          setRun((prev) => ({
            ...prev,
            done: index + 1,
            needsReview: failures.length,
            failures: [...failures],
          }));
          continue;
        }
        resumed = true;
        const response = await publish.mutateAsync({
          items: [{ warehouseId: String(item.productId) }],
          accountId: saved.accountId,
          shopId: saved.shopId,
          confirm: true,
          claimOnly: false,
        });
        submitted += response.published.length;
        await recordPublishMutation.mutateAsync({
          runId: saved.id,
          itemId: item.id,
          status: response.published.length > 0 ? "needs_review"
            : response.failures.length > 0 ? "failed" : "needs_review",
          reason: response.failures.map((failure) => failure.reason).filter(Boolean).join("；")
            || (response.published.length > 0
              ? "妙手已受理续发，等待最终发布回执。"
              : "妙手未返回可核实的发布结果，请核对发布记录。"),
          result: {
            batchId: response.batchId,
            published: response.published,
            failures: response.failures,
          },
        });
        if (response.published.length > 0 || response.failures.length > 0) {
          failures.push({
            key: target.key,
            title: target.title || "",
            detailId: target.detailId,
            reason: response.published.length > 0
              ? "妙手已受理续发，最终状态以发布记录回执为准。"
              : response.failures.map((failure) => failure.reason).filter(Boolean).join("；")
                || "发布未完成，请核对发布记录。",
          });
        }
      } catch (error) {
        const reason = apiErrorMessage(error);
        if (resumed) {
          try {
            await recordPublishMutation.mutateAsync({
              runId: saved.id, itemId: item.id, status: "needs_review", reason,
            });
          } catch { /* Recovery keeps ambiguous publish submissions from being retried. */ }
        }
        if (!resumed) {
          retryableKeys.add(target.key);
          setPublishQueued(target.key);
        }
        failures.push({ key: target.key, title: target.title || "", detailId: target.detailId, reason });
      }
      setRun((prev) => ({
        ...prev,
        done: index + 1,
        needsReview: failures.length,
        failures: [...failures],
      }));
    }
    setRun((prev) => ({
      ...prev, phase: "done", endedAt: Date.now(),
      stage: submitted > 0 ? `已提交续发 ${submitted} 件，等待妙手回执`
        : failures.length > 0 ? `${failures.length} 项需检查` : "续发完成",
    }));
    // 仅移除已尝试提交的项目；规格确认时跳过或启动前失败的项目仍可重试。
    setSelection((current) => new Map([...current].filter(([key]) => retryableKeys.has(key))));
    if (failures.length > 0) {
      toast.warning(`续发完成，${failures.length} 项需要检查`, {
        description: failures.map((failure) => failure.reason).join("；"),
      });
    } else if (submitted > 0) {
      toast.info(`已提交续发 ${submitted} 件`, {
        description: "平台最终状态以发布记录回执为准。",
      });
    }
  };

  const executeRun = async (): Promise<void> => {
    if (!connected) {
      toast.warning("请先连接妙手");
      return;
    }
    const targets = [...selection.values()];
    if (targets.length === 0) {
      toast.warning("请先勾选要处理的商品");
      return;
    }
    if (targets.length > MAX_RUN_ITEMS) {
      toast.warning(`一次最多处理 ${MAX_RUN_ITEMS} 件商品`, {
        description: "请取消部分选择后再开始任务。",
      });
      return;
    }
    if (publishConfigIssue) {
      toast.warning(publishConfigIssue);
      return;
    }
    const savedRun = latestRunQuery.data?.run;
    if (latestRunQuery.isLoading || recoverRunMutation.isPending
      || recoveringRunId.current !== null
      || (savedRun && hydratedRunId.current !== savedRun.id)) {
      toast.info("正在恢复上次任务，请稍后再开始");
      return;
    }
    const resumable = savedRun?.items.filter((item) => pendingAutomationPublishAction(item) !== null) ?? [];
    const resumableKeys = new Set(resumable.map((item) => item.key));
    if (targets.some((target) => resumableKeys.has(target.key))) {
      if (targets.some((target) => !resumableKeys.has(target.key))) {
        toast.warning("请只选择同一任务中待发布或续发的商品");
        return;
      }
      if (!savedRun) return;
      await resumePublishTargets(targets, savedRun);
      return;
    }
    let durableRun: AutomationRunRecord | null = null;
    if (persistentRunId.current && savedRun?.id === persistentRunId.current) {
      const queued = new Set(savedRun.items.filter((item) => item.processStatus === "queued").map((item) => item.key));
      const previous = new Map(savedRun.items.map((item) => [item.key, item]));
      if (targets.some((target) => {
        const item = previous.get(target.key);
        return item?.processStatus === "needs_review"
          || item?.publishStatus === "needs_review"
          || item?.publishStatus === "running";
      })) {
        toast.warning("所选商品存在待核对结果，请先核对妙手处理或发布记录");
        return;
      }
      if (targets.some((target) => previous.get(target.key)?.processStatus === "succeeded"
        && previous.get(target.key)?.publishStatus !== "queued")) {
        toast.warning("所选商品已有成功处理记录，请取消已完成商品后再开始新任务");
        return;
      }
      const resumesSavedRun = targets.length > 0 && targets.every((target) => queued.has(target.key));
      if (resumesSavedRun) {
        durableRun = savedRun;
      } else if (targets.some((target) => queued.has(target.key))) {
        toast.warning("请单独选择上次任务的待处理商品，或取消选择后开始新任务");
        return;
      }
    }
    if (!durableRun) {
      // 新任务使用当前页面上下文；恢复任务则完全使用下方的持久化快照。
      if (settings.publishEnabled && !currentShopId) {
        toast.warning("请先在顶部选择当前店铺");
        return;
      }
      if (settings.publishEnabled && !shopsQuery.data?.shops.some((shop) => shop.shopId === currentShopId)) {
        toast.warning("当前店铺不可用，请重新选择店铺");
        return;
      }
      const targetShopId = currentShopId ?? targets[0].shopId;
      try {
        durableRun = (await createRunMutation.mutateAsync({
          accountId: status?.accountId ?? "",
          shopId: targetShopId,
          settings: { ...settings },
          items: targets.map(({ key, detailId, shopId, title }) => ({ key, detailId, shopId, title })),
        })).run;
      } catch (error) {
        toast.error(`无法保存任务：${apiErrorMessage(error)}`);
        return;
      }
      persistentRunId.current = durableRun.id;
      hydratedRunId.current = durableRun.id;
      persistentItems.current = new Map(durableRun.items.map((item) => [item.key, item.id]));
    }

    const runSettings = {
      ...DEFAULT_SETTINGS,
      ...Object.fromEntries(Object.entries(durableRun.settings).filter(([key, value]) =>
        key in DEFAULT_SETTINGS && typeof value === typeof DEFAULT_SETTINGS[key as keyof AutomationSettings])),
    } as AutomationSettings;
    if (runSettings.publishEnabled && (
      !runSettings.textEnabled || !runSettings.imagesEnabled
      || !runSettings.priceTemplateEnabled || !runSettings.template?.trim()
    )) {
      toast.warning("此任务的发布配置不完整，请检查文字、图片和定价模板设置后重新创建任务。");
      return;
    }
    const imagesPerProduct = runSettings.mainImageCount + runSettings.skuImageCount + runSettings.detailImageCount;
    const plannedImages = runSettings.imagesEnabled ? targets.length * imagesPerProduct : 0;
    stopRef.current = false;
    setStages(Object.fromEntries(targets.map((target) => [target.key, "queued" as ItemStage])));
    setRun({
      phase: "running",
      total: targets.length,
      done: 0,
      failed: 0,
      startedAt: Date.now(),
      endedAt: null,
      imageDone: 0,
      imagePlanned: plannedImages,
      stage: "准备处理…",
      failures: [],
      failedKeys: [],
    });

    let done = 0;
    let failed = 0;
    let imageDone = 0;
    let textDone = 0;
    let imageFailures = 0;
    let publishIssues = 0;
    let publishSubmitted = 0;
    const failedKeys: string[] = [];
    const failures: RunFailure[] = [];
    const skippedPublishKeys = new Set<string>();

    for (const target of targets) {
      if (stopRef.current) {
        setRun((prev) => ({
          ...prev,
          phase: "stopped",
          done,
          failed,
          imageDone,
          failedKeys,
          stage: "已手动停止",
          endedAt: Date.now(),
        }));
        toast.info(`已停止：完成 ${done} 件，成功 ${done - failed} 件`);
        return;
      }

      setStages((prev) => ({ ...prev, [target.key]: "running" }));
      setRun((prev) => ({
        ...prev,
        stage: `处理中：${target.title || target.detailId}`,
      }));

      let processResponseReceived = false;
      try {
        const runId = persistentRunId.current;
        const itemId = persistentItems.current.get(target.key);
        if (!runId || !itemId) throw new Error("任务记录缺少商品明细，已停止以免丢失执行状态");
        await startRunItemMutation.mutateAsync({ runId, itemId });
        // 串行 await：一件处理完再下一件，避免后端并发写库
        const result = await processItem.mutateAsync({
          detailId: target.detailId,
          request: {
            shopId: target.shopId,
            accountId: durableRun.accountId,
            language: runSettings.textEnabled ? runSettings.language : null,
            template:
              runSettings.priceTemplateEnabled && runSettings.template ? runSettings.template : null,
            save: true,
            // 勾了「图片」才提交翻译；不勾就是纯文字处理
            processImages: runSettings.imagesEnabled,
            mainImageCount: runSettings.mainImageCount,
            skuImageCount: runSettings.skuImageCount,
            detailImageCount: runSettings.detailImageCount,
          },
        });
        processResponseReceived = true;
        if (runSettings.publishEnabled && (
          result.saved !== true
          || result.product?.draftVerified !== true
          || !result.product?.id
        )) {
          throw new Error("商品草稿未通过保存与读回核验，已阻止发布；请检查妙手商品记录后再继续。");
        }
        const failedImages = runSettings.imagesEnabled && (result.images?.failed ?? 0) > 0;
        const imageFailureReason = failedImages
          ? `图片处理失败 ${result.images?.failed} 张，已阻止发布；请先重试失败图片并核对结果。`
          : "";
        await recordProcessMutation.mutateAsync({
          runId, itemId, status: failedImages ? "failed" : "succeeded",
          reason: imageFailureReason,
          productId: result.product?.id ? String(result.product.id) : null,
          result: {
            textDone: runSettings.textEnabled,
            images: result.images ?? null,
            saved: result.saved,
            draftVerified: result.product?.draftVerified,
          },
        });
        // 只有真的提交了翻译任务才算「图片处理完成」。
        // 之前这里加的是商品自带图片数，勾不勾「图片」数字都涨，等于假数据。
        imageDone += result.images?.submitted ?? 0;
        if (runSettings.imagesEnabled && result.images && result.images.failed > 0) {
          imageFailures += result.images.failed;
        }
        setStages((prev) => ({ ...prev, [target.key]: "done" }));
        // 记下这一步到底做成了什么：文字译了没、图片交上去几张、失败几张。
        // 只记一个 done，界面就没法区分「全好了」和「文字好了图片全挂」。
        if (runSettings.textEnabled) textDone += 1;
        setProgress((prev) => ({
          ...prev,
          [target.key]: {
            textDone: runSettings.textEnabled,
            imagesDone: runSettings.imagesEnabled
              ? (result.images?.failed ?? 0) === 0 && (result.images?.submitted ?? 0) > 0
              : false,
            imageFailures: result.images?.failed ?? 0,
          },
        }));

        if (failedImages) {
          failed += 1;
          failedKeys.push(target.key);
          failures.push({
            key: target.key,
            title: target.title || "",
            detailId: target.detailId,
            reason: imageFailureReason,
          });
          setStages((prev) => ({ ...prev, [target.key]: "failed" }));
          done += 1;
          setRun((prev) => ({
            ...prev,
            done,
            failed,
            imageDone,
            textDone,
            failedKeys: [...failedKeys],
            failures: [...failures],
          }));
          continue;
        }

        // 异步图片任务尚未回写处理结果时，不发布旧图版本。
        if (runSettings.publishEnabled) {
          const warehouseId = result.product?.id;
          if (warehouseId) {
        const pendingImages = (result.images?.submitted ?? 0) > 0;
            if (pendingImages) {
              const reason = "图片翻译任务仍在处理中，已阻止发布旧图片版本；图片完成后请核对并重新处理发布。";
              try {
                await recordPublishMutation.mutateAsync({ runId, itemId, status: "needs_review", reason });
              } catch {
                // 恢复接口会把“处理成功、发布仍排队”的项目转为待核对。
              }
              publishIssues += 1;
              failures.push({
                key: target.key,
                title: (target.title || "") + "（等待图片处理）",
                detailId: target.detailId,
                reason,
              });
            } else {
              setRun((prev) => ({ ...prev, stage: `正式发布：${target.title || target.detailId}` }));
              try {
                const started = await startOrResumePublish({
                  runId,
                  itemId,
                  productId: String(warehouseId),
                  title: target.title || target.detailId,
                  resume: false,
                });
                if (started === "skipped") {
                  publishIssues += 1;
                  skippedPublishKeys.add(target.key);
                  setPublishQueued(target.key);
                  failures.push({
                    key: target.key,
                    title: (target.title || "") + "（已跳过发布）",
                    detailId: target.detailId,
                    reason: "已跳过本次发布；商品保留在待发布队列，可再次选择后继续。",
                  });
                  done += 1;
                  setRun((prev) => ({
                    ...prev,
                    done,
                    imageDone,
                    textDone,
                    failures: [...failures],
                  }));
                  continue;
                }
                const response = await publish.mutateAsync({
                  items: [{ warehouseId: String(warehouseId) }],
                  accountId: durableRun.accountId,
                  shopId: durableRun.shopId,
                  confirm: true,
                  claimOnly: false,
                });
                publishSubmitted += response.published.length;
                const accepted = response.published.length > 0;
                await recordPublishMutation.mutateAsync({
                  runId, itemId,
                  status: accepted ? "needs_review" : response.failures.length > 0 ? "failed" : "needs_review",
                  reason: response.failures.map((item) => item.reason).filter(Boolean).join("；")
                    || (accepted ? "妙手已受理，等待最终发布回执。" : "妙手未返回可核实的发布结果，请核对发布记录。"),
                  result: {
                    batchId: response.batchId,
                    published: response.published,
                    failures: response.failures,
                  },
                });
                if (accepted || response.failures.length > 0) {
                  setStages((prev) => ({
                    ...prev,
                    [target.key]: accepted ? "needs_review" : "failed",
                  }));
                }
                if (accepted || response.failures.length > 0) {
                  publishIssues += 1;
                  failures.push({
                    key: target.key,
                    title: (target.title || "") + (accepted ? "（等待发布回执）" : "（发布失败）"),
                    detailId: target.detailId,
                    reason: accepted
                      ? "妙手已受理发布请求，最终状态以发布记录回执为准。"
                      : response.failures.map((item) => item.reason).filter(Boolean).join("；")
                        || "妙手没有返回已提交商品明细，请到发布记录核对。",
                  });
                }
              } catch (publishError) {
                const reason = apiErrorMessage(publishError);
                try {
                  await recordPublishMutation.mutateAsync({ runId, itemId, status: "needs_review", reason });
                } catch { /* 保留服务端 running 状态，页面恢复时会转成待核对 */ }
                publishIssues += 1;
                failures.push({
                  key: target.key,
                  title: (target.title || "") + "（发布失败）",
                  detailId: target.detailId,
                  reason,
                });
              }
            }
          } else {
            const reason = "商品处理结果缺少仓库商品 ID，无法确认发布目标。";
            try {
              await recordPublishMutation.mutateAsync({ runId, itemId, status: "needs_review", reason });
            } catch {
              // 下次打开时由恢复接口关闭未开始的发布项。
            }
            publishIssues += 1;
            failures.push({
              key: target.key,
              title: (target.title || "") + "（发布待核对）",
              detailId: target.detailId,
              reason,
            });
          }
        }
      } catch (error) {
        const uncertain = processResponseReceived;
        if (uncertain) {
          setRun((prev) => ({ ...prev, needsReview: (prev.needsReview ?? 0) + 1 }));
        } else {
          failed += 1;
          failedKeys.push(target.key);
        }
        const reason = apiErrorMessage(error);
        const runId = persistentRunId.current;
        const itemId = persistentItems.current.get(target.key);
        if (runId && itemId) {
          try {
            await recordProcessMutation.mutateAsync({
              runId, itemId, status: uncertain ? "needs_review" : "failed", reason,
            });
          } catch { /* 遗留 running 项在下次打开时会进入待核对 */ }
        }
        // 留住明细：toast 三秒就没了，失败原因必须能在页面上回看
        failures.push({
          key: target.key,
          title: target.title || "",
          detailId: target.detailId,
          reason,
        });
        setStages((prev) => ({ ...prev, [target.key]: uncertain ? "needs_review" : "failed" }));
      }

      done += 1;
      setRun((prev) => ({
        ...prev,
        done,
        failed,
        imageDone,
        textDone,
        failedKeys: [...failedKeys],
        failures: [...failures],
      }));
    }

    const succeeded = targets.length - failed;
    setRun((prev) => ({
      ...prev,
      phase: "done",
      done,
      failed,
      imageDone,
      textDone,
      failedKeys,
      stage: failures.length > 0
        ? `处理完成，${failures.length} 项需检查`
        : publishSubmitted > 0
          ? `已提交发布 ${publishSubmitted} 件，等待妙手回执`
          : "全部完成",
      endedAt: Date.now(),
    }));
    setSelection((prev) => {
      const next = new Map(prev);
      for (const target of targets) {
        if (!skippedPublishKeys.has(target.key)) next.delete(target.key);
      }
      return next;
    });

    if (imageFailures > 0) {
      toast.warning(`有 ${imageFailures} 张图没能提交翻译`, {
        description: "商品已入库；图片翻译可以稍后在仓库详情里重试。",
      });
    }

    if (failed > 0 || publishIssues > 0) {
      toast.warning(`已处理 ${succeeded} 件，${failed + publishIssues} 项需要检查`, {
        description: [
          failed > 0 ? `${failed} 件处理失败` : null,
          publishIssues > 0 ? `${publishIssues} 件发布未完成或需核对` : null,
          "失败明细保留在右侧「处理状态」面板，可筛选「失败」逐条重试",
        ]
          .filter(Boolean)
          .join("；"),
      });
    } else {
      toast.info(
        publishSubmitted > 0
          ? `已处理并提交发布 ${publishSubmitted} 件`
          : `已处理 ${succeeded} 件`,
        publishSubmitted > 0
          ? { description: "平台最终状态以发布记录回执为准。" }
          : undefined
      );
    }
  };

  const startRun = async (): Promise<void> => {
    if (startingRunRef.current) return;
    startingRunRef.current = true;
    setPreparingRun(true);
    try {
      await executeRun();
    } finally {
      startingRunRef.current = false;
      setPreparingRun(false);
    }
  };

  const stopRun = (): void => {
    stopRef.current = true;
    setRun((prev) => ({ ...prev, stage: "停止中，等待当前这件完成…" }));
  };

  /* ---------------- 弹窗 ---------------- */
  // 定价模板的两个弹窗（选用 / 编辑）互斥：同时渲染两个 Dialog 会叠层，
  // 且共用 z-[50]，前一个退场动画被卡住就永远盖不掉了。用枚举保证只有一个。
  const [templateDialog, setTemplateDialog] = useState<"none" | "pick" | "edit">("none");
  const templatesOpen = templateDialog === "pick";
  const setTemplatesOpen = (open: boolean): void => setTemplateDialog(open ? "pick" : "none");
  const hasPublishNeedsReview = savedRun?.items.some((item) => item.publishStatus === "needs_review") ?? false;
  const isResumeSelection = selection.size > 0 && [...selection.keys()].every((key) =>
    savedRun?.items.some((item) => item.key === key && pendingAutomationPublishAction(item) !== null));

  return (
    <>
      <PageHeader
        title="自动化上品"
        actions={
          <span className="flex items-center gap-2">
            {/* 账号 / 店铺切换挪到「开始处理」旁边（老版是标题右侧一组） */}
            <AccountSwitcher compact />
            {running ? (
              <Button variant="danger" icon={<Square />} onClick={stopRun}>
                停止
              </Button>
            ) : (
              <Button
                variant="primary"
                icon={<Play />}
                disabled={!connected || selection.size === 0
                  || (!isResumeSelection && (!somethingEnabled || Boolean(publishConfigIssue)))
                  || preparingRun || latestRunQuery.isLoading || recoverRunMutation.isPending}
                loading={preparingRun}
                title={
                  !connected
                    ? "先连接妙手"
                    : selection.size === 0
                      ? "先勾选要处理的商品"
                    : preparingRun || latestRunQuery.isLoading || recoverRunMutation.isPending
                      ? "正在准备任务或恢复上次任务"
                      : isResumeSelection
                      ? `检查图片和待确认规格后继续发布 ${selection.size} 件商品`
                      : !somethingEnabled
                        ? "文字和图片处理都关着，没有可执行的内容"
                        : publishConfigIssue
                          ? publishConfigIssue
                        : `串行处理 ${selection.size} 件商品`
                }
                onClick={() => void startRun()}
              >
                {isResumeSelection ? "继续发布选中商品" : "开始处理"}
              </Button>
            )}
          </span>
        }
      />

      {/* PageContent 外层默认 display:block，这里改成 flex 列，内层才能被撑成一屏高度 */}
      <PageContent
        width="full"
        scroll={scrollLayout}
        padding="p-4"
        className={cn(
          "flex flex-col [&>div]:flex [&>div]:min-h-0 [&>div]:flex-1 [&>div]:flex-col [&>div]:gap-3",
          scrollLayout && "[&>div]:min-h-max [&>div]:flex-none",
          scrollLayout ? "overflow-y-auto" : "overflow-hidden"
        )}
      >
        <div className="flex min-h-0 flex-1 flex-col gap-3">
          {/* 左主操作区 + 右侧固定进度列。本页不再放「连接妙手」面板：
              左侧导航已有独立板块，这里重复一份只会占掉列表高度。 */}
          <div className={cn(
            "grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1fr)_320px]",
            scrollLayout && "min-h-max flex-none",
          )}>
            {/* 左：选品（弹性撑满剩余高度）+ 处理设置（自然高度，超高自己滚） */}
            <div className={cn(
              "flex min-h-0 min-w-0 flex-col gap-3 overflow-hidden max-lg:overflow-visible",
              scrollLayout && "min-h-max flex-none overflow-visible",
            )}>
              <ProductPicker
                className={cn(
                  "min-h-[300px] min-w-0 flex-1 max-lg:flex-none",
                  scrollLayout && "flex-none",
                )}
                connected={connected}
                items={visibleItems}
                total={total}
                hasMore={hasMore}
                page={page}
                pageSize={pageSize}
                loading={listQuery.isLoading}
                fetching={listQuery.isFetching}
                error={listQuery.error}
                visibleCount={visibleItems.length}
                filter={filter}
                selectedKeys={selectedKeys}
                stages={stages}
                progress={progress}
                disabled={running}
                onFilterChange={(next) => {
                  setFilter(next);
                  setPage(1);
                }}
                onRefresh={() => void refetchList()}
                onRetry={() => void refetchList()}
                onPageChange={setPage}
                onPageSizeChange={(next) => {
                  setPageSize(next);
                  setPage(1);
                }}
                onToggle={toggleItem}
                onSelectVisible={selectVisible}
              />

              {/* 处理设置：自然高度，窗口矮时在卡片内滚动。
                  没有第二个「开始处理」按钮 —— 老版只有标题栏那一个。 */}
              <Card
                padding="normal"
                className="shrink-0 overflow-y-auto max-lg:max-h-none max-lg:overflow-visible"
                style={scrollLayout ? undefined : { maxHeight: "calc(100% - 312px)" }}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="flex min-w-0 items-center gap-2">
                    <CardTitle>处理设置</CardTitle>
                    <Badge variant="neutral" size="sm">
                      已选 {formatCount(selection.size)} 件
                    </Badge>
                  </span>
                </div>

                <div className="mt-3">
                  <ProcessSettings
                    settings={settings}
                    onChange={patchSettings}
                    connected={connected}
                    onOpenTemplates={() => setTemplatesOpen(true)}
                    publishShopName={publishShopName}
                    disabled={running}
                  />
                </div>
              </Card>
            </div>

            {/* 右：固定 320px 进度列。
                DiagnosePanel 移除 —— 用户说运行日志/诊断没用，
                失败明细已经在 ProgressPanel 里留了，诊断面板是重复信息。
                以前这里是 overflow-y-auto，内容一更新（进度百分比跳动）浏览器就会
                把滚动位置往最近的锚点拽，看起来就是「右边的栏自己在动」。
                改成 overflow-hidden + overscroll-contain，列表再变化也不动。 */}
            <div className={cn(
              "min-h-0 overflow-hidden overscroll-contain pb-1 max-lg:shrink-0 max-lg:overflow-visible",
              scrollLayout && "overflow-visible",
            )}>
              {hasPublishNeedsReview && (
                <div className="mb-2 flex justify-end">
                  <Button
                    variant="secondary"
                    size="sm"
                    icon={<RefreshCw className={cn(refreshPublishLogsMutation.isPending && "animate-spin")} />}
                    loading={refreshPublishLogsMutation.isPending}
                    loadingText="刷新中"
                    onClick={() => {
                      void (async () => {
                        try {
                          const result = await refreshPublishLogsMutation.mutateAsync();
                          const refreshed = await latestRunQuery.refetch();
                          const recovered = refreshed.data?.run;
                          const retryable = recovered?.items.filter((item) =>
                            pendingAutomationPublishAction(item) === "retry") ?? [];
                          if (retryable.length > 0) {
                            setSelection((current) => {
                              const next = new Map(current);
                              for (const item of retryable) {
                                next.set(item.key, {
                                  key: item.key,
                                  detailId: item.detailId,
                                  shopId: item.shopId,
                                  title: item.title,
                                });
                              }
                              return next;
                            });
                          }
                          if (result.degraded) toast.warning("暂时无法读取妙手发布记录");
                          else if (result.updated > 0) toast.success(`已更新 ${result.updated} 条发布回执`);
                          else toast.info("暂未匹配到最终回执，商品仍需核对");
                        } catch (error) {
                          toast.warning(apiErrorMessage(error));
                        }
                      })();
                    }}
                  >
                    刷新回执
                  </Button>
                </div>
              )}
              <ProgressPanel
                run={run}
                settings={settings}
                selectedCount={selection.size}
                visibleCount={visibleItems.length}
                publishShopName={shopsQuery.data?.shops.find((shop) => shop.shopId === currentShopId)?.name}
              />
            </div>
          </div>
        </div>
      </PageContent>

      {/* 定价模板：这里只做选用，编辑走 PriceTemplateDialog */}
      <Dialog
        open={templatesOpen}
        onOpenChange={setTemplatesOpen}
        size="md"
        title="定价模板"
        description="选一个模板用在这一批上，或者点「编辑模板」改参数。"
        footer={
          <>
            <Button variant="ghost" onClick={() => setTemplatesOpen(false)}>
              关闭
            </Button>
            <Button
              variant="primary"
              icon={<Pencil />}
              onClick={() => setTemplateDialog("edit")}
            >
              编辑模板
            </Button>
          </>
        }
      >
        {templatesQuery.isLoading && <SkeletonRows rows={3} />}

        {!templatesQuery.isLoading && templatesError && (
          <InlineError onRetry={() => void templatesQuery.refetch()}>{templatesError}</InlineError>
        )}

        {!templatesQuery.isLoading && !templatesError && templateNames.length === 0 && (
          <EmptyState
            icon={<Tag strokeWidth={1.5} />}
            title="还没有可用的定价模板"
            description="在服务端数据目录新增模板文件后，这里会自动出现。"
            minHeight={140}
          />
        )}

        {templateNames.length > 0 && (
          <ul className="flex flex-col gap-1.5">
            {templateNames.map((name) => (
              <li key={name}>
                <button
                  type="button"
                  onClick={() => {
                    patchSettings({ priceTemplateEnabled: true, template: name });
                    setTemplatesOpen(false);
                    toast.success(`已选用定价模板「${name}」`);
                  }}
                  className="flex w-full items-center justify-between gap-2 rounded-md border border-border-subtle px-3 py-2 text-left text-[13px] transition-colors duration-fast hover:border-border-strong hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32"
                >
                  <span className="truncate text-fg">{name}</span>
                  {settings.template === name && settings.priceTemplateEnabled ? (
                    <Badge variant="brand" size="sm">
                      使用中
                    </Badge>
                  ) : (
                    <span className="shrink-0 text-2xs text-fg-muted">选用</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}

        {templatesQuery.data?.templates.some((template) => template.invalid) && (
          <p className="mt-3 text-2xs leading-4 text-warning-700">
            有模板因格式问题被跳过：
            {templatesQuery.data.templates
              .filter((template) => template.invalid)
              .map((template) => template.name)
              .join("、")}
          </p>
        )}

        {run.phase !== "idle" && (
          <p className="mt-3 text-2xs leading-4 text-fg-muted">
            上一次批次开始于{" "}
            {run.startedAt ? DATE_FORMAT.format(new Date(run.startedAt)) : "—"}
            ，共 {formatCount(run.total)} 件，失败 {formatCount(run.failed)} 件。
          </p>
        )}
      </Dialog>

      <PriceTemplateDialog
        open={templateDialog === "edit"}
        onOpenChange={(open) => setTemplateDialog(open ? "edit" : "none")}
      />

      <Dialog
        open={Boolean(specificationPrompt)}
        onOpenChange={(open) => { if (!open) resolveSpecificationPrompt(null); }}
        size="md"
        title="确认商品规格"
        description={specificationPrompt?.title ?? ""}
        closeOnOverlay={false}
        footer={
          <>
            <Button variant="ghost" onClick={() => resolveSpecificationPrompt(null)}>
              跳过本件
            </Button>
            <Button
              variant="primary"
              disabled={!specificationQuery.data?.product || typeof specificationQuery.data.product.revision !== "number"
                || specificationQuery.isFetching || specificationQuery.isError}
              onClick={() => {
                const product = specificationQuery.data?.product;
                if (!product || typeof product.revision !== "number") return;
                resolveSpecificationPrompt({
                  confirmedProductId: String(product.id),
                  confirmedRevision: product.revision,
                });
              }}
            >
              确认当前版本并继续
            </Button>
          </>
        }
      >
        {specificationQuery.isLoading && <SkeletonRows rows={3} />}
        {specificationQuery.isError && (
          <InlineError onRetry={() => void specificationQuery.refetch()}>
            {apiErrorMessage(specificationQuery.error)}
          </InlineError>
        )}
        {specificationPrompt?.stale && (
          <p className="mb-3 rounded-md bg-warning-50 px-3 py-2 text-xs leading-5 text-warning-700">
            商品在确认前发生变化，已重新读取当前版本，请再次核对。
          </p>
        )}
        {specificationQuery.data && (() => {
          const product = specificationQuery.data.product;
          const payload = product.payload ?? {};
          const reasons = (product.qualityReviewReasons ?? []).map((reason) => QUALITY_REASON_LABELS[reason] ?? reason);
          return (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={product.qualityReviewStatus === "verified" ? "success" : "warning"}>
                  {product.qualityReviewStatus === "verified" ? "当前版本已核验" : "规格待核验"}
                </Badge>
                <span className="text-xs text-fg-muted">当前版本 v{product.revision ?? "未知"}</span>
              </div>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 rounded-md border border-border-subtle bg-surface-muted p-3 text-sm">
                <dt className="text-fg-muted">商品重量</dt>
                <dd className="text-fg">{measureLabel(payload.weight, "kg")}</dd>
                <dt className="text-fg-muted">包裹尺寸</dt>
                <dd className="text-fg">
                  {[payload.packageLength, payload.packageWidth, payload.packageHeight]
                    .map((value) => measureLabel(value, "cm")).join(" × ")}
                </dd>
                {reasons.length > 0 && <>
                  <dt className="text-fg-muted">待核验项</dt>
                  <dd className="text-warning-700">{reasons.join("、")}</dd>
                </>}
              </dl>
              <p className="text-xs leading-5 text-fg-muted">
                请根据商品资料核对重量和包裹尺寸。确认只对上方显示的当前版本生效，不会修改规格数据；跳过后该商品仍留在待发布队列。
              </p>
            </div>
          );
        })()}
      </Dialog>
    </>
  );
}

export default AutomationPage;
