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
//   · 跨页全选 —— 后端 /collect-box 不支持按筛选条件批量选中，只能选本页可见结果
//   · 采集箱每页 >20 —— 服务端校验 pageSize 必须 1–20，超出会 400

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pencil, Play, Square, Tag } from "lucide-react";
import { apiErrorMessage } from "@/lib/api";
import { formatCount } from "@/lib/utils";
import {
  useAuthStatus,
  useCollectBoxList,
  useProcessCollectItem,
  usePublish,
  useShops,
  useTemplates,
  useWorkflowDefaults,
  useSaveWorkflowDefaults,
} from "@/lib/queries";
import { useSizeCharts, pickCharts, pickDefaultUrl, buildDefaultSizeChart } from "@/features/automation/useSizeCharts";
import { PriceTemplateDialog } from "@/features/warehouse/PriceTemplateDialog";
import type { CollectBoxItem } from "@/lib/types";
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
import { ProductPicker } from "@/features/automation/ProductPicker";
import { ProgressPanel } from "@/features/automation/ProgressPanel";
import { AccountSwitcher } from "@/features/auth/UnlockPage";
import {
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

/** 自动扫描间隔（扫描即查询，没有独立的 scan 接口） */
const AUTO_SCAN_MS = 30_000;

const DATE_FORMAT = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

export function AutomationPage() {
  const { data: status } = useAuthStatus();
  const connected = status?.connected ?? false;

  /* ---------------- 列表查询参数 ---------------- */
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [keyword, setKeyword] = useState("");
  const [filter, setFilter] = useState<ListFilter>("all");
  const [autoScan, setAutoScan] = useState(false);

  const listQuery = useCollectBoxList(
    { page, pageSize, status: "all", keyword: keyword.trim() || undefined },
    connected
  );

  const items = useMemo<CollectBoxItem[]>(() => listQuery.data?.items ?? [], [listQuery.data?.items]);
  const total = listQuery.data?.total ?? 0;
  const hasMore = listQuery.data?.hasMore ?? false;

  // 自动扫描：定时重取（「扫描即查询」）
  const refetchList = listQuery.refetch;
  useEffect(() => {
    if (!autoScan || !connected) return;
    const timer = window.setInterval(() => void refetchList(), AUTO_SCAN_MS);
    return () => window.clearInterval(timer);
  }, [autoScan, connected, refetchList]);

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
  }, []);

  const clearSelection = useCallback(() => setSelection(new Map()), []);

  /* ---------------- 本地处理态 ---------------- */
  const [stages, setStages] = useState<Record<string, ItemStage>>({});
  // 每件商品完成了哪几步（老版 productStatus 的 textCompleted / imagesCompleted）
  const [progress, setProgress] = useState<Record<string, ItemProgress>>({});
  const [run, setRun] = useState<RunState>(IDLE_RUN);
  const running = run.phase === "running";
  // 两个开关都关着就没有可执行内容：按钮置灰并说明原因（老版 summary-warning）
  const somethingEnabled = settings.textEnabled || settings.imagesEnabled;
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
      case "all":
      default:
        return items;
    }
  }, [filter, items, selectedKeys, stages]);

  const selectVisible = useCallback(() => {
    setSelection((prev) => {
      const next = new Map(prev);
      for (const item of visibleItems) {
        if (!next.has(item.key)) {
          next.set(item.key, {
            key: item.key,
            detailId: item.detailId,
            shopId: item.shopId,
            title: item.title,
          });
        }
      }
      return next;
    });
  }, [visibleItems]);

  /* ---------------- 真正执行：串行处理入仓 ---------------- */
  const processItem = useProcessCollectItem();
  const publish = usePublish();
  const { data: authData } = useAuthStatus();
  const shopsQuery = useShops(authData?.accountId, { enabled: connected && settings.publishEnabled });

  const startRun = async (): Promise<void> => {
    if (!connected) {
      toast.warning("请先连接妙手");
      return;
    }
    const targets = [...selection.values()];
    if (targets.length === 0) {
      toast.warning("请先勾选要处理的商品");
      return;
    }
    // 发布开启但没选目标店铺：先拦住，别等处理完才发现没地方认领
    if (settings.publishEnabled && !settings.publishShopId) {
      toast.warning("请先选择要发布到哪家店铺", {
        description: "在处理设置里勾选「发布到店铺」后，点文字选择目标店铺。",
      });
      return;
    }

    const plannedImages = settings.imagesEnabled ? targets.length * settings.mainImageCount : 0;
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
    const failedKeys: string[] = [];
    const failures: RunFailure[] = [];

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

      try {
        // 串行 await：一件处理完再下一件，避免后端并发写库
        const result = await processItem.mutateAsync({
          detailId: target.detailId,
          request: {
            shopId: target.shopId,
            language: settings.textEnabled ? settings.language : null,
            template:
              settings.priceTemplateEnabled && settings.template ? settings.template : null,
            save: true,
            // 勾了「图片」才提交翻译；不勾就是纯文字处理
            processImages: settings.imagesEnabled,
            mainImageCount: settings.mainImageCount,
          },
        });
        // 只有真的提交了翻译任务才算「图片处理完成」。
        // 之前这里加的是商品自带图片数，勾不勾「图片」数字都涨，等于假数据。
        imageDone += result.images?.submitted ?? 0;
        if (settings.imagesEnabled && result.images && result.images.failed > 0) {
          imageFailures += result.images.failed;
        }
        setStages((prev) => ({ ...prev, [target.key]: "done" }));
        // 记下这一步到底做成了什么：文字译了没、图片交上去几张、失败几张。
        // 只记一个 done，界面就没法区分「全好了」和「文字好了图片全挂」。
        if (settings.textEnabled) textDone += 1;
        setProgress((prev) => ({
          ...prev,
          [target.key]: {
            textDone: settings.textEnabled,
            imagesDone: settings.imagesEnabled
              ? (result.images?.failed ?? 0) === 0 && (result.images?.submitted ?? 0) > 0
              : false,
            imageFailures: result.images?.failed ?? 0,
          },
        }));

        // 发布到店铺：处理成功后自动认领到所选店铺的采集箱（不直接上架）。
        // 这是用户要的效果——处理完的东西出现在妙手采集箱里，人工确认后再上架。
        if (settings.publishEnabled && settings.publishShopId) {
          const warehouseId = result.product?.id;
          if (warehouseId) {
            setRun((prev) => ({ ...prev, stage: `认领到店铺：${target.title || target.detailId}` }));
            try {
              await publish.mutateAsync({
                items: [{ warehouseId: String(warehouseId) }],
                shopId: settings.publishShopId,
                confirm: true,
                claimOnly: true,
              });
            } catch (claimError) {
              // 认领失败不算处理失败：商品已在仓库里，可以稍后从仓库手动认领。
              // 但要让用户知道这件事没做成，不能假装一切都好。
              const reason = apiErrorMessage(claimError);
              toast.warning(`「${target.title || target.detailId}」已入仓，但认领到店铺失败`, {
                description: reason,
              });
              failures.push({
                key: target.key,
                title: (target.title || "") + "（认领失败）",
                detailId: target.detailId,
                reason: `入仓成功，认领失败：${reason}`,
              });
            }
          }
        }
      } catch (error) {
        failed += 1;
        failedKeys.push(target.key);
        const reason = apiErrorMessage(error);
        // 留住明细：toast 三秒就没了，失败原因必须能在页面上回看
        failures.push({
          key: target.key,
          title: target.title || "",
          detailId: target.detailId,
          reason,
        });
        setStages((prev) => ({ ...prev, [target.key]: "failed" }));
        toast.error(`处理失败：${target.title || target.detailId}`, {
          description: reason,
        });
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
      stage: failed > 0 ? `完成，${failed} 件失败` : "全部完成",
      endedAt: Date.now(),
    }));

    if (imageFailures > 0) {
      toast.warning(`有 ${imageFailures} 张图没能提交翻译`, {
        description: "商品已入库；图片翻译可以稍后在仓库详情里重试。",
      });
    }

    if (failed > 0) {
      toast.warning(`已处理 ${succeeded} 件，${failed} 件失败`, {
        description: "可在筛选里选「失败」逐条重试",
      });
    } else {
      toast.success(`已处理入仓 ${succeeded} 件`);
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
                disabled={!connected || selection.size === 0 || !somethingEnabled}
                title={
                  !connected
                    ? "先连接妙手"
                    : selection.size === 0
                      ? "先勾选要处理的商品"
                      : !somethingEnabled
                        ? "文字和图片处理都关着，没有可执行的内容"
                        : `串行处理 ${selection.size} 件商品`
                }
                onClick={() => void startRun()}
              >
                开始处理
              </Button>
            )}
          </span>
        }
      />

      {/* PageContent 外层默认 display:block，这里改成 flex 列，内层才能被撑成一屏高度 */}
      <PageContent
        width="full"
        scroll={false}
        padding="p-4"
        className="flex flex-col overflow-hidden [&>div]:flex [&>div]:min-h-0 [&>div]:flex-1 [&>div]:flex-col [&>div]:gap-3"
      >
        <div className="flex min-h-0 flex-1 flex-col gap-3">
          {/* 左主操作区 + 右侧固定进度列。本页不再放「连接妙手」面板：
              左侧导航已有独立板块，这里重复一份只会占掉列表高度。 */}
          <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_320px] gap-3">
            {/* 左：选品（弹性撑满剩余高度）+ 处理设置（自然高度，超高自己滚） */}
            <div className="flex min-h-0 min-w-0 flex-col gap-3 overflow-hidden">
              <ProductPicker
                className="min-h-[200px] min-w-0 flex-1"
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
                keyword={keyword}
                autoScan={autoScan}
                selectedKeys={selectedKeys}
                stages={stages}
                progress={progress}
                disabled={running}
                onFilterChange={(next) => {
                  setFilter(next);
                  setPage(1);
                }}
                onKeywordChange={(next) => {
                  setKeyword(next);
                  setPage(1);
                }}
                onAutoScanChange={setAutoScan}
                onScan={() => void refetchList()}
                onRetry={() => void refetchList()}
                onPageChange={setPage}
                onPageSizeChange={(next) => {
                  setPageSize(next);
                  setPage(1);
                }}
                onToggle={toggleItem}
                onSelectVisible={selectVisible}
                onClearSelection={clearSelection}
              />

              {/* 处理设置：自然高度，窗口矮时在卡片内滚动。
                  没有第二个「开始处理」按钮 —— 老版只有标题栏那一个。 */}
              <Card
                padding="normal"
                className="shrink-0 overflow-y-auto"
                style={{ maxHeight: "calc(100% - 212px)" }}
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
                    shops={shopsQuery.data?.shops ?? []}
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
            <div className="min-h-0 overflow-hidden overscroll-contain pb-1">
              <ProgressPanel
                run={run}
                settings={settings}
                selectedCount={selection.size}
                visibleCount={visibleItems.length}
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
    </>
  );
}

export default AutomationPage;
