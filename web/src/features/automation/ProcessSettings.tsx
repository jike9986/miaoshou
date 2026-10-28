// features/automation —— 处理设置（老版「操作」区重点，一屏内可见）
//
// 结构（紧凑网格排布，不靠滚动也能看完）—— 这是原本就验证过的两列布局，不要动：
//   左列：1. 商品文字语言 + 四个开关同排（文字 / 图片 / 定价模板 / 发布到店铺）
//         2. 主图数量 / 详情图数量 / 图片并发数
//         3. 重量增加 + 包裹长宽高
//         4. 图片处理开关
//   右列：5. 尺码图横向缩略图 gallery（点击选中、双击放大、添加/删除）
//
// 四个开关同排是老版 index.html 的 switch-group 原样：
//   【文字】【图片】【定价模板】【发布到店铺】横向一组，各自带一个文字按钮。
//
// 数据来源（全部为现有真实接口）：
//   · 定价模板 GET /system/templates   → useTemplates
//   · 尺码图   GET /size-charts        → useSizeCharts（本目录，走 @/lib/api）
//   · 删除尺码图 DELETE /size-charts/{id}（按 id）或 DELETE /size-charts（按 url）
//   · 上传尺码图 POST /size-charts/upload：文件走二进制分支，链接走 JSON 分支
//
// 列表响应里的 defaultUrl 是「项目默认尺码图」地址，一条本地记录都没有时也能用。
// 后端按地址删时会拒绝删这条（400「项目默认尺码图不能删除」），所以界面必须
// 在点下去之前就拦住——让用户先看到 400 再解释，属于让人白点一次。

import { useRef, useState } from "react";
import {
  ImagePlus,
  Images,
  Link2,
  Maximize2,
  RefreshCw,
  Ruler,
  Trash2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { apiErrorMessage } from "@/lib/api";
import {
  Badge,
  Button,
  Checkbox,
  ConfirmDialog,
  Dialog,
  ErrorState,
  Field,
  IconButton,
  InlineError,
  Input,
  NumberInput,
  Select,
  SkeletonRows,
  Switch,
  toast,
} from "@/components/ui";
import {
  SIZE_CHART_ACCEPT,
  SIZE_CHART_MAX_BYTES,
  PROJECT_DEFAULT_CHART_NAME,
  buildDefaultSizeChart,
  isDefaultSizeChart,
  pickCharts,
  pickDefaultUrl,
  sizeChartErrorMessage,
  useDeleteSizeChart,
  useDeleteSizeChartByUrl,
  useSizeCharts,
  useUploadSizeChart,
} from "@/features/automation/useSizeCharts";
import {
  LANGUAGE_OPTIONS,
  type AutomationSettings,
  type SizeChart,
} from "@/features/automation/types";
import { ImageServiceCard } from "@/features/automation/ImageServiceCard";

export interface ProcessSettingsProps {
  settings: AutomationSettings;
  onChange: (patch: Partial<AutomationSettings>) => void;
  /** 已连接妙手：未连接时不发读请求，写操作全部禁用 */
  connected: boolean;
  /** 打开定价模板弹窗（GET /system/templates） */
  onOpenTemplates: () => void;
  /** 跳转成品仓库——真正的发布确认弹窗（PublishDialog）在那里，
   *  它只接受 warehouseId，本页手里只有 detailId，所以走跳转而不在本页造弹窗 */
  onOpenPublish: () => void;
  /** 运行中：全部控件禁用 */
  disabled?: boolean;
  className?: string;
}

export function ProcessSettings({
  settings,
  onChange,
  connected,
  onOpenTemplates,
  onOpenPublish,
  disabled = false,
  className,
}: ProcessSettingsProps) {
  const chartsQuery = useSizeCharts(connected);
  const deleteChart = useDeleteSizeChart();
  // 按地址删：老接口形态 + 项目默认图保护的兜底路径（默认图那条会被 UI 拦在前面）
  const deleteChartByUrl = useDeleteSizeChartByUrl();
  const uploadChart = useUploadSizeChart();
  const charts = pickCharts(chartsQuery.data);
  const defaultUrl = pickDefaultUrl(chartsQuery.data);
  // 兜底地址可能是「本地一条记录都没有」，这时也要能预览
  const fallbackChart = buildDefaultSizeChart(defaultUrl);
  const [preview, setPreview] = useState<SizeChart | null>(null);
  // 待删除的尺码图：删除走确认弹窗，而不是点一下就没了（忘书签可恢复成本高）
  const [pendingDelete, setPendingDelete] = useState<SizeChart | null>(null);
  // 尺码图：本地文件真上传（POST /size-charts/upload 二进制分支）
  // 或只给地址（同一接口的 JSON 分支）
  const [addOpen, setAddOpen] = useState(false);
  /** 添加弹窗的当前模式：file = 选本地图片，url = 用链接添加 */
  const [addMode, setAddMode] = useState<"file" | "url">("file");
  const [addName, setAddName] = useState("");
  const [addUrl, setAddUrl] = useState("");
  const [addFile, setAddFile] = useState<File | null>(null);
  const [addError, setAddError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const locked = disabled || !connected;
  const deletePending = deleteChart.isPending || deleteChartByUrl.isPending;
  const uploadPending = uploadChart.isPending;

  /** 当前选中的那条是不是项目默认图（是则不允许删） */
  const selectedChart = charts.find((item) => item.id === settings.sizeChartId) ?? null;
  const selectedIsDefault = isDefaultSizeChart(selectedChart, defaultUrl);

  /**
   * 删除：有 id 走 id（后端按 id 删不看默认图那套规则，但我们已在 UI 拦过），
   * 没有 id（老接口形态 / 只有地址的记录）走新的按 URL 删除。
   */
  const removeChart = (chart: SizeChart): void => {
    if (isDefaultSizeChart(chart, defaultUrl)) {
      // 后端会 400「项目默认尺码图不能删除」，不必真发这一枪
      toast.warning("项目默认尺码图不能删除");
      return;
    }
    const onSuccess = (name: string, count = 1): void => {
      if (chart.id && chart.id === settings.sizeChartId) onChange({ sizeChartId: null });
      toast.success(`已从尺码图库移除「${name}」`, {
        description:
          count > 1
            ? `同地址登记了 ${count} 条，一并忘掉；图床上的图片仍然保留`
            : "只忘掉书签，图床上的图片仍然保留",
      });
      setPendingDelete(null);
    };
    const onError = (error: unknown): void => {
      toast.error("删除尺码图失败", { description: sizeChartErrorMessage(error) });
    };

    if (chart.id) {
      deleteChart.mutate(chart.id, {
        onSuccess: (result) => onSuccess(result.name || chart.name),
        onError,
      });
      return;
    }
    deleteChartByUrl.mutate(chart.url, {
      onSuccess: (result) => onSuccess(result.name || chart.name, result.count),
      onError,
    });
  };

  const removeSelected = (): void => {
    if (!selectedChart) return;
    if (selectedIsDefault) {
      toast.warning("项目默认尺码图不能删除");
      return;
    }
    setPendingDelete(selectedChart);
  };
  // 开关组合校验（老版 updateTaskSummary 的 summary-warning）。
  // 全关时点「开始处理」什么都不会发生，但按钮是亮的——
  // 用户会以为在跑，等半天发现没动。运行前就把话说清楚。
  const comboWarning = ((): string | null => {
    if (!settings.textEnabled && !settings.imagesEnabled) {
      return "文字处理和图片处理都关着，没有可执行的内容。";
    }
    if (settings.priceTemplateEnabled && !settings.textEnabled) {
      return "定价模板需要先开启文字处理，否则不会生效。";
    }
    if (settings.publishEnabled) {
      return "发布已开启：保存后会直接上架到店铺，不可撤销。";
    }
    return null;
  })();
  const comboDanger = settings.publishEnabled && Boolean(comboWarning);

  return (    <>
      <div
        className={cn(
          // 单列纵向：上方控件区铺满整宽（四个开关才放得开），
          // 尺码图单独占底下一整行。两列时尺码图被压进 260px 窄列，
          // 缩略图小到看不清内容，而尺码图的价值就是"能看清对不对"。
          "flex flex-col gap-3",
          className
        )}
      >
        {/* ===================== 控件区 ===================== */}
        <div className="flex min-w-0 flex-col gap-3">
          {comboWarning && (
            <p
              className={cn(
                "rounded-md border px-2.5 py-1.5 text-2xs leading-4",
                comboDanger
                  ? "border-danger-600/20 bg-danger-50 text-danger-700"
                  : "border-warning-600/20 bg-warning-50 text-warning-700"
              )}
            >
              {comboWarning}
            </p>
          )}
          {/* 1. 语言 + 四个开关同排（老版 switch-group 原样） */}
          <div className="flex flex-col gap-2">
            <Field label="商品文字语言" className="w-[150px] shrink-0">
              <Select
                value={settings.language}
                onChange={(value) =>
                  onChange({ language: value as AutomationSettings["language"] })
                }
                options={LANGUAGE_OPTIONS.map((option) => ({
                  value: option.value,
                  label: option.label,
                }))}
                disabled={locked || !settings.textEnabled}
              />
            </Field>

            <div className="flex flex-nowrap items-center gap-x-2.5">
              <span className="flex h-6 shrink-0 items-center">
                <Switch
                  checked={settings.textEnabled}
                  onChange={(checked) => onChange({ textEnabled: checked })}
                  label="文字"
                  disabled={disabled}
                />
              </span>
              <span className="flex h-6 shrink-0 items-center">
                <Switch
                  checked={settings.imagesEnabled}
                  onChange={(checked) => onChange({ imagesEnabled: checked })}
                  label="图片"
                  disabled={disabled}
                />
              </span>

              <span className="flex h-6 shrink-0 items-center gap-1.5 whitespace-nowrap">
                <Checkbox
                  checked={settings.priceTemplateEnabled}
                  onChange={(checked) => onChange({ priceTemplateEnabled: checked })}
                  label="定价模板"
                  size="sm"
                  disabled={disabled}
                />
                <button
                  type="button"
                  onClick={onOpenTemplates}
                  className="text-xs leading-[18px] text-brand-700 underline-offset-2 transition-colors duration-fast hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32"
                >
                  选择
                </button>
              </span>

              <span className="flex h-6 shrink-0 items-center gap-1.5 whitespace-nowrap">
                <Checkbox
                  checked={settings.publishEnabled}
                  onChange={(checked) => onChange({ publishEnabled: checked })}
                  label="发布到店铺"
                  size="sm"
                  disabled={disabled}
                />
                <button
                  type="button"
                  onClick={onOpenPublish}
                  className="text-xs leading-[18px] text-brand-700 underline-offset-2 transition-colors duration-fast hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32"
                >
                  设置
                </button>
              </span>
            </div>
          </div>

          {/* 2. 图片数量 + 并发 */}
          <div className="grid grid-cols-3 gap-3">
            <Field label="主图数量">
              <NumberInput
                value={settings.mainImageCount}
                onChange={(value) => onChange({ mainImageCount: value ?? 1 })}
                min={1}
                max={9}
                step={1}
                decimals={0}
                suffix="张"
                disabled={disabled}
              />
            </Field>
            <Field label="详情图数量">
              <NumberInput
                value={settings.detailImageCount}
                onChange={(value) => onChange({ detailImageCount: value ?? 1 })}
                min={1}
                max={9}
                step={1}
                decimals={0}
                suffix="张"
                disabled={disabled}
              />
            </Field>
            <Field label="图片并发数">
              <NumberInput
                value={settings.imageConcurrency}
                onChange={(value) => onChange({ imageConcurrency: value ?? 1 })}
                min={1}
                max={9}
                step={1}
                decimals={0}
                suffix="并发"
                disabled={disabled}
              />
            </Field>
          </div>

          {/* 2.5 图片服务：密钥配没配、从哪读的、要不要花钱验一次。
              放在数量设置下面、重量设置上面 —— 它在逻辑上属于「图片」这一组，
              而重量/包裹尺寸是物流参数，隔开更符合阅读顺序。 */}
          <ImageServiceCard disabled={disabled} />

          {/* 3. 重量 + 包裹尺寸 */}
          <div className="grid grid-cols-2 gap-3">
            <Field label="重量增加">
              <NumberInput
                value={settings.weightExtraKg}
                onChange={(value) => onChange({ weightExtraKg: value ?? 0 })}
                min={0}
                max={5}
                step={0.01}
                decimals={2}
                suffix="kg"
                disabled={disabled}
              />
            </Field>
            <Field label="包裹长">
              <NumberInput
                value={settings.lengthCm}
                onChange={(value) => onChange({ lengthCm: value ?? 0.01 })}
                min={0.01}
                max={300}
                step={0.01}
                decimals={2}
                suffix="cm"
                disabled={disabled}
              />
            </Field>
            <Field label="包裹宽">
              <NumberInput
                value={settings.widthCm}
                onChange={(value) => onChange({ widthCm: value ?? 0.01 })}
                min={0.01}
                max={300}
                step={0.01}
                decimals={2}
                suffix="cm"
                disabled={disabled}
              />
            </Field>
            <Field label="包裹高">
              <NumberInput
                value={settings.heightCm}
                onChange={(value) => onChange({ heightCm: value ?? 0.01 })}
                min={0.01}
                max={300}
                step={0.01}
                decimals={2}
                suffix="cm"
                disabled={disabled}
              />
            </Field>
          </div>
        </div>

        {/* ===================== 尺码图（整行） ===================== */}
        <div className="flex min-w-0 flex-col gap-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="flex shrink-0 items-center gap-1.5 whitespace-nowrap text-sm font-medium leading-[18px] text-fg-secondary">
              <Ruler className="size-3.5 text-fg-muted" strokeWidth={1.75} />
              尺码图
            </span>
            <div className="flex shrink-0 items-center gap-1.5">
              <Button
                size="sm"
                variant="secondary"
                icon={<ImagePlus className="size-3.5" />}
                disabled={locked}
                onClick={() => {
                  setAddName("");
                  setAddUrl("");
                  setAddFile(null);
                  setAddError(null);
                  setAddMode("file");
                  setAddOpen(true);
                }}
              >
                添加尺码图
              </Button>
              {/* disabled 的 Button 带 pointer-events-none，鼠标事件落不到它身上，
                  title 必须挂在父级 span 才能弹出「为什么点不了」 */}
              <span
                className="inline-flex"
                title={
                  selectedIsDefault
                    ? "项目默认尺码图不能删除"
                    : settings.sizeChartId
                      ? undefined
                      : "先在下面选一张尺码图"
                }
              >
                <Button
                  size="sm"
                  variant="secondary"
                  icon={<Trash2 className="size-3.5" />}
                  // 项目默认图后端不让删（按地址删会 400）：按钮直接禁用并说明原因，
                  // 别让用户点了才收到「项目默认尺码图不能删除」。
                  disabled={locked || !settings.sizeChartId || selectedIsDefault || deletePending}
                  loading={deletePending}
                  onClick={removeSelected}
                >
                  删除所选
                </Button>
              </span>
              <IconButton
                label="刷新尺码图"
                size="sm"
                variant="ghost"
                icon={<RefreshCw className={cn(chartsQuery.isFetching && "animate-spin")} />}
                onClick={() => void chartsQuery.refetch()}
                disabled={!connected}
              />
            </div>
          </div>

          {connected && chartsQuery.isLoading && <SkeletonRows rows={2} />}

          {connected && !chartsQuery.isLoading && chartsQuery.isError && (
            <ErrorState
              title="尺寸表读取失败"
              description={apiErrorMessage(chartsQuery.error)}
              onRetry={() => void chartsQuery.refetch()}
              retrying={chartsQuery.isFetching}
              minHeight={84}
            />
          )}

          {/* 空库但有兜底地址：不能只说「尺码图库是空的」——后端仍会拿项目
              默认图去用，用户以为没图可用、实际发布时用的是那张。 */}
          {connected &&
            !chartsQuery.isLoading &&
            !chartsQuery.isError &&
            charts.length === 0 &&
            fallbackChart && (
              <div className="flex min-h-[84px] flex-1 flex-wrap items-center gap-3 rounded-md border border-border bg-canvas-inset/40 px-3 py-2">
                <button
                  type="button"
                  // 兜底地址没有后端 id，选中无意义，点一下直接看大图
                  onClick={() => setPreview(fallbackChart)}
                  title={`${PROJECT_DEFAULT_CHART_NAME}（点击放大）`}
                  disabled={disabled}
                  className={cn(
                    "size-[76px] shrink-0 overflow-hidden rounded-md border border-border bg-canvas-inset",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32",
                    disabled && "cursor-not-allowed opacity-60"
                  )}
                >
                  <img
                    src={fallbackChart.url}
                    alt={PROJECT_DEFAULT_CHART_NAME}
                    loading="lazy"
                    draggable={false}
                    referrerPolicy="no-referrer"
                    className="size-full object-cover"
                  />
                </button>
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="flex items-center gap-1.5">
                    <Badge variant="brand" size="sm">
                      项目默认
                    </Badge>
                    <span className="text-xs leading-[18px] text-fg-secondary">
                      当前使用项目默认尺码图
                    </span>
                  </span>
                  <p className="break-all font-mono text-2xs leading-4 text-fg-muted">
                    {fallbackChart.url}
                  </p>
                  <p className="text-xs leading-[18px] text-fg-muted">
                    本地还没有自己的尺码图，处理时后端会自动带上这张；添加自己的图后会优先用自己的。
                  </p>
                </div>
              </div>
            )}

          {connected &&
            !chartsQuery.isLoading &&
            !chartsQuery.isError &&
            charts.length === 0 &&
            !fallbackChart && (
              <div className="flex min-h-[84px] flex-1 items-center justify-center gap-2 rounded-md border border-dashed border-border bg-canvas-inset/40 text-xs text-fg-muted">
                <Images className="size-4" strokeWidth={1.5} />
                还没有尺码图
              </div>
            )}

          {connected && charts.length > 0 && (
            <div className="flex min-h-[84px] flex-1 flex-wrap content-start gap-2">
              {charts.map((chart) => {
                const active = chart.id === settings.sizeChartId;
                const isDefault = isDefaultSizeChart(chart, defaultUrl);
                return (
                  <div
                    key={chart.id}
                    className={cn(
                      "group relative flex w-[120px] shrink-0 flex-col overflow-hidden rounded-md border bg-canvas-inset",
                      "transition-[border-color,box-shadow] duration-fast ease-in-out",
                      active
                        ? "border-brand-600 ring-2 ring-brand-600/24"
                        : "border-border hover:border-border-strong",
                      disabled && "opacity-60"
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => onChange({ sizeChartId: active ? null : chart.id })}
                      onDoubleClick={() => setPreview(chart)}
                      title={chart.name}
                      aria-pressed={active}
                      disabled={disabled}
                      className={cn(
                        "relative flex w-full flex-col",
                        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32",
                        disabled && "cursor-not-allowed"
                      )}
                    >
                      <img
                        src={chart.url}
                        alt={chart.name}
                        loading="lazy"
                        draggable={false}
                        referrerPolicy="no-referrer"
                        className="aspect-[4/5] w-full object-cover"
                      />
                      <span className="absolute inset-x-0 bottom-0 truncate bg-inverse/85 px-1 py-0.5 text-[10px] leading-4 text-fg-inverse">
                        {chart.name}
                      </span>
                      <span className="absolute right-1 top-1 opacity-0 transition-opacity duration-fast group-hover:opacity-100">
                        <Maximize2 className="size-3 text-fg-inverse drop-shadow" strokeWidth={2.2} />
                      </span>
                      {active && (
                        <span className="absolute left-1 top-1">
                          <Badge variant="brand" size="sm">
                            已选
                          </Badge>
                        </span>
                      )}
                    </button>

                    {/* 项目默认图：标出来 + 删除按钮禁用，原因写在 title 里 */}
                    <div className="flex items-center justify-between gap-1 border-t border-border-subtle px-1 py-1">
                      {isDefault ? (
                        <Badge
                          variant="brand"
                          size="sm"
                          title="项目默认尺码图不能删除"
                        >
                          项目默认
                        </Badge>
                      ) : (
                        <span />
                      )}
                      <IconButton
                        label={
                          isDefault ? "项目默认尺码图不能删除" : `删除尺码图「${chart.name}」`
                        }
                        title={
                          isDefault ? "项目默认尺码图不能删除" : `删除尺码图「${chart.name}」`
                        }
                        variant={isDefault ? "ghost" : "danger"}
                        size="sm"
                        icon={<Trash2 />}
                        disabled={locked || isDefault || deletePending}
                        onClick={() => {
                          // 默认图那条删除按钮是 disabled 的，正常点不到；
                          // 这里再拦一次是因为 disabled 状态下仍可能被键盘/脚本触发
                          if (isDefault) {
                            toast.warning("项目默认尺码图不能删除");
                            return;
                          }
                          setPendingDelete(chart);
                        }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* 本地有自己的图，但库里的默认图那条不在列表里：兜底地址仍然生效，
              这里补一行说明，免得用户以为「默认图已经没了」。 */}
          {connected &&
            charts.length > 0 &&
            fallbackChart &&
            !charts.some((chart) => isDefaultSizeChart(chart, defaultUrl)) && (
              <p className="flex flex-wrap items-center gap-1.5 text-xs leading-[18px] text-fg-muted">
                <Badge variant="brand" size="sm">
                  项目默认
                </Badge>
                <span className="break-all font-mono text-2xs">{fallbackChart.url}</span>
                <span>站点没有自己的尺码图时，后端会用它兜底</span>
              </p>
            )}
        </div>
      </div>

      {/* 添加尺码图：两条分支同一个接口（POST /size-charts/upload），
          选文件走二进制分支，用链接添加走 JSON 分支。 */}
      <Dialog
        open={addOpen}
        onOpenChange={(open) => {
          if (!open) setAddOpen(false);
        }}
        size="md"
        title="添加尺码图"
        description={
          addMode === "file"
            ? "选择本地图片会直接上传到图床。单张不超过 8 MB，支持 PNG / JPEG / WebP。"
            : "贴一个已经在图床上的图片地址。地址需以 http:// 或 https:// 开头，内网地址后端会拒绝。"
        }
        footer={
          <>
            <Button variant="ghost" onClick={() => setAddOpen(false)}>
              取消
            </Button>
            <Button
              variant="primary"
              loading={uploadPending}
              disabled={addMode === "file" ? !addFile : !addUrl.trim()}
              onClick={() => {
                setAddError(null);

                // --- 分支一：本地文件上传 ---
                if (addMode === "file") {
                  if (!addFile) return setAddError("请选择一张图片");
                  const name = addName.trim() || addFile.name.replace(/\.[^.]+$/, "");
                  if (!name) return setAddError("请填写名称");
                  uploadChart.mutate(
                    { file: addFile, name },
                    {
                      onSuccess: (chart) => {
                        toast.success(`已上传尺码图「${chart.name}」`);
                        setAddOpen(false);
                        setAddFile(null);
                      },
                      // 后端 422 的 message（「文件类型与声明不一致：声明 X，实际 Y」）
                      // 原样显示，吞成「上传失败」会让用户朝错误的方向重试
                      onError: (error) => setAddError(sizeChartErrorMessage(error)),
                    }
                  );
                  return;
                }

                // --- 分支二：只给地址（JSON 分支，后端做内网拦截） ---
                const url = addUrl.trim();
                // 前端只挡「明显不是 http(s) 地址」这一层，内网判断交给后端
                if (!/^https?:\/\//i.test(url)) {
                  return setAddError("图片地址需以 http:// 或 https:// 开头");
                }
                const name = addName.trim();
                uploadChart.mutate(
                  { url, ...(name ? { name } : {}) },
                  {
                    onSuccess: (chart) => {
                      toast.success(`已添加尺码图「${chart.name}」`);
                      setAddOpen(false);
                    },
                    onError: (error) => setAddError(sizeChartErrorMessage(error)),
                  }
                );
              }}
            >
              {addMode === "file" ? "上传" : "添加"}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          {/* 两个入口并排：默认选文件，点「用链接添加」切到 URL 输入。
              后端两条分支共用一个接口，这里切模式只是换输入框。 */}
          <div className="flex items-center gap-1.5">
            <Button
              size="sm"
              variant={addMode === "file" ? "secondary" : "ghost"}
              icon={<ImagePlus className="size-3.5" />}
              disabled={uploadPending}
              onClick={() => {
                setAddMode("file");
                setAddError(null);
              }}
            >
              上传本地图片
            </Button>
            <Button
              size="sm"
              variant={addMode === "url" ? "secondary" : "ghost"}
              icon={<Link2 className="size-3.5" />}
              disabled={uploadPending}
              onClick={() => {
                setAddMode("url");
                setAddError(null);
              }}
            >
              用链接添加
            </Button>
          </div>

          <Field
            label="名称"
            hint={
              addMode === "file"
                ? "留空则用文件名"
                : "留空则由后端按地址末段取名"
            }
          >
            <Input
              value={addName}
              onChange={(event) => setAddName(event.target.value)}
              placeholder="如：通用尺码表"
              maxLength={128}
              disabled={uploadPending}
            />
          </Field>

          {addMode === "file" ? (
            <Field label="本地图片" hint="选中后会直接上传到图床">
              <div className="flex items-center gap-2">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  className="hidden"
                  onChange={(event) => {
                    const picked = event.target.files?.[0] ?? null;
                    setAddError(null);
                    if (!picked) {
                      setAddFile(null);
                      return;
                    }
                    if (!SIZE_CHART_ACCEPT.includes(picked.type)) {
                      setAddError("请选择 PNG、JPEG 或 WebP 图片");
                      setAddFile(null);
                      return;
                    }
                    if (picked.size > SIZE_CHART_MAX_BYTES) {
                      setAddError("尺码图须为不超过 8 MB 的图片");
                      setAddFile(null);
                      return;
                    }
                    setAddFile(picked);
                    if (!addName.trim()) {
                      setAddName(picked.name.replace(/\.[^.]+$/, ""));
                    }
                  }}
                />
                <Button
                  size="sm"
                  variant="secondary"
                  icon={<ImagePlus className="size-3.5" />}
                  disabled={uploadPending}
                  onClick={() => fileInputRef.current?.click()}
                >
                  选择图片
                </Button>
                <span className="min-w-0 flex-1 truncate text-xs text-fg-muted">
                  {addFile
                    ? `${addFile.name}（${(addFile.size / 1024).toFixed(0)} KB）`
                    : "未选择文件"}
                </span>
                {addFile && (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={uploadPending}
                    onClick={() => setAddFile(null)}
                  >
                    移除
                  </Button>
                )}
              </div>
            </Field>
          ) : (
            <Field
              label="图片地址"
              hint="只能填公网 http(s) 地址；内网 / 本机地址后端会拒绝并给出原因"
            >
              <Input
                value={addUrl}
                onChange={(event) => setAddUrl(event.target.value)}
                placeholder="https://..."
                maxLength={2048}
                disabled={uploadPending}
                invalid={Boolean(addError)}
              />
            </Field>
          )}

          {addError && <InlineError>{addError}</InlineError>}
        </div>
      </Dialog>

      {/* 删除确认：写清影响对象，并说明「只忘书签、图片保留」 */}
      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open && !deletePending) setPendingDelete(null);
        }}
        title="删除尺码图？"
        description={
          pendingDelete
            ? `将从尺码图库移除「${pendingDelete.name || pendingDelete.url}」。`
            : undefined
        }
        confirmLabel="删除"
        danger
        loading={deletePending}
        onConfirm={() => {
          if (pendingDelete) removeChart(pendingDelete);
        }}
      >
        <p className="text-[13px] leading-5 text-fg-muted">
          只忘掉这条书签，图床上的图片仍然保留；已经用它上架的商品不受影响。
        </p>
      </ConfirmDialog>

      {/* 尺码图放大预览（双击触发）。预览的也可能是「兜底地址」——
          那条没有真实 id，不能当作选中项写进 settings。 */}
      <Dialog
        open={preview !== null}
        onOpenChange={(open) => {
          if (!open) setPreview(null);
        }}
        size="lg"
        title={preview?.name ?? "尺码图"}
        description={
          preview && isDefaultSizeChart(preview, defaultUrl)
            ? "项目默认尺码图 · 由后端在本地图库为空时兜底使用"
            : preview?.site
              ? `站点 ${preview.site}`
              : "通用尺码图"
        }
        footer={
          <>
            <Button variant="secondary" onClick={() => setPreview(null)}>
              关闭
            </Button>
            <Button
              variant="primary"
              // 兜底地址没有后端 id：选它只会写进一个空 id，处理时后端仍按自己的规则取图
              disabled={preview === null || disabled || !preview.id}
              title={preview && !preview.id ? "这是后端兜底地址，无需选中" : undefined}
              onClick={() => {
                if (preview?.id) onChange({ sizeChartId: preview.id });
                setPreview(null);
              }}
            >
              选这张
            </Button>
          </>
        }
      >
        {preview && (
          <div className="flex flex-col gap-3">
            <div className="grid max-h-[52vh] place-items-center overflow-auto rounded-md border border-border bg-canvas-inset p-2">
              <img
                src={preview.url}
                alt={preview.name}
                referrerPolicy="no-referrer"
                className="max-h-[48vh] w-auto object-contain"
              />
            </div>
            <p className="break-all font-mono text-2xs leading-4 text-fg-muted">{preview.url}</p>
          </div>
        )}
      </Dialog>
    </>
  );
}

export default ProcessSettings;
