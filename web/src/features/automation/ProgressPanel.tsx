// features/automation —— 右侧固定宽度进度栏（老版「监视」区）
//
// 只做展示：数据全部由 AutomationPage 通过 props 传入（RunState + 设置快照），
// 不在这里发起任何请求，也不编造数据 —— 没有真实运行时就显示
// 「等待开始」「0 / 0」，不会出现假进度。
//
// 结构：
//   1. 处理状态卡：当前阶段 / 运行时间 / 预计剩余 / 批次进度 + 两条进度条
//   2. 任务摘要卡：已选商品、文字处理、图片处理、定价模板、发布到店铺、目标语言

import { useEffect, useState } from "react";
import { Activity, Gauge, ListChecks } from "lucide-react";
import { cn, formatCount, formatDuration } from "@/lib/utils";
import { Badge, Card, CardTitle, Progress, Separator, StatusBadge } from "@/components/ui";
import {
  languageLabel,
  type AutomationSettings,
  type RunState,
} from "@/features/automation/types";

export interface ProgressPanelProps {
  run: RunState;
  settings: AutomationSettings;
  /** 当前选中件数（勾选集合大小，含跨页） */
  selectedCount: number;
  /** 列表里本页可见件数 */
  visibleCount: number;
  className?: string;
}

/** 运行中每秒 +1；非运行态返回 null（不显示跳动数字） */
function useTickingElapsed(startedAt: number | null, active: boolean): number | null {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active || startedAt === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, startedAt]);

  if (startedAt === null) return null;
  return Math.max(0, Math.round((now - startedAt) / 1000));
}

export function ProgressPanel({
  run,
  settings,
  selectedCount,
  visibleCount,
  className,
}: ProgressPanelProps) {
  const running = run.phase === "running";
  const elapsed = useTickingElapsed(run.startedAt, running);

  // 已结束的批次按 endedAt 冻结耗时
  const finalElapsed =
    run.startedAt !== null && run.endedAt !== null
      ? Math.max(0, Math.round((run.endedAt - run.startedAt) / 1000))
      : null;
  const shownElapsed = running ? elapsed : finalElapsed;

  // 预计剩余：只在运行中且有已完成样本时估算，避免编造
  const remaining =
    running && shownElapsed !== null && run.done > 0 && run.total > run.done
      ? Math.round((shownElapsed / run.done) * (run.total - run.done))
      : null;

  const batchPercent = run.total > 0 ? (run.done / run.total) * 100 : 0;
  // 图片进度：按「已处理件数 × 每件主图数」推进；没有计划量时显示 0
  const imageDone = run.imageDone;
  const imagePercent = run.imagePlanned > 0 ? (imageDone / run.imagePlanned) * 100 : 0;

  const textDone = run.done;
  const textPercent = settings.textEnabled && run.total > 0 ? (textDone / run.total) * 100 : 0;

  const phaseMeta =
    run.phase === "running"
      ? { label: "运行中", tone: "info" as const, dot: "info" as const, breathe: true }
      : run.phase === "done"
        ? { label: "已完成", tone: "success" as const, dot: "success" as const }
        : run.phase === "stopped"
          ? { label: "已停止", tone: "warning" as const, dot: "warning" as const }
          : { label: "等待开始", tone: "neutral" as const, dot: "neutral" as const };

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {/* 处理状态 ------------------------------------------------------- */}
      <Card padding="normal">
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-1.5">
            <Activity className="size-4 text-fg-muted" strokeWidth={1.75} />
            <CardTitle>处理状态</CardTitle>
          </span>
          <StatusBadge meta={phaseMeta} />
        </div>

        <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2.5">
          <Stat label="当前阶段" value={run.stage} wide />
          <Stat
            label="运行时间"
            value={shownElapsed === null ? "等待开始" : formatDuration(shownElapsed)}
          />
          <Stat label="预计剩余" value={remaining === null ? "—" : formatDuration(remaining)} />
          <Stat
            label="批次进度"
            value={
              run.total > 0
                ? `${formatCount(run.done)} / ${formatCount(run.total)}（${Math.round(batchPercent)}%）`
                : "0 / 0"
            }
            wide
          />
        </div>

        <Separator className="my-3" />

        <div className="flex flex-col gap-3">
          <Progress
            value={textPercent}
            tone={settings.textEnabled ? "brand" : "warning"}
            label="商品设置与文字"
            showValue
          />
          <p className="num -mt-1.5 text-2xs text-fg-muted">
            {settings.textEnabled
              ? `${formatCount(textDone)} / ${formatCount(run.total)} 件 · 目标 ${languageLabel(settings.language)}`
              : "已关闭：不翻译标题与属性"}
            {/* 文字和图片是两个独立开关，各自成功了几件要分开说。
                合成一个「已完成 N 件」会把「文字好了图片全挂」盖掉。 */}
            {run.phase === "done" && settings.imagesEnabled && (
              <span className="ml-1 text-fg-subtle">
                （文字 {formatCount(run.textDone ?? 0)} 件、图片 {formatCount(run.imageDone)} 张）
              </span>
            )}
          </p>

          <Progress
            value={imagePercent}
            tone={settings.imagesEnabled ? "brand" : "warning"}
            label="图片处理"
            showValue
          />
          <p className="num -mt-1.5 text-2xs text-fg-muted">
            {settings.imagesEnabled
              ? `${formatCount(imageDone)} / ${formatCount(run.imagePlanned)} 张`
              : "已关闭：不处理主图与详情图"}
          </p>
        </div>

        {run.failed > 0 && (
          <div className="mt-3 flex flex-col gap-2">
            <p className="rounded-md border border-warning-600/20 bg-warning-50 px-2.5 py-1.5 text-2xs leading-4 text-warning-700">
              本批失败 {formatCount(run.failed)} 件
              {run.failures.length > 0 ? `，原因见下方` : "，可在列表中筛「失败」逐条重试"}
            </p>
            {/* 失败明细（老版 failures-section）：toast 会消失，这里留住原因 */}
            {run.failures.length > 0 && (
              <ul className="flex flex-col gap-1">
                {run.failures.map((failure) => (
                  <li
                    key={failure.key}
                    className="rounded border border-warning-200 bg-warning-50/60 px-2 py-1.5"
                  >
                    <p
                      className="truncate text-2xs font-medium text-fg"
                      title={failure.title || failure.detailId}
                    >
                      {failure.title || failure.detailId}
                    </p>
                    <p className="mt-0.5 break-words text-2xs leading-4 text-warning-700">
                      {failure.reason}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </Card>

      {/* 任务摘要 ------------------------------------------------------- */}
      <Card padding="normal">
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-1.5">
            <ListChecks className="size-4 text-fg-muted" strokeWidth={1.75} />
            <CardTitle>任务摘要</CardTitle>
          </span>
          <Gauge className="size-4 text-fg-muted" strokeWidth={1.75} />
        </div>

        <dl className="mt-3 flex flex-col gap-2">
          <SummaryRow label="已选商品" value={`${formatCount(selectedCount)} 件`} />
          <SummaryRow label="本页可见" value={`${formatCount(visibleCount)} 件`} muted />
          <Separator className="my-0.5" />
          <SummaryRow
            label="文字处理"
            value={settings.textEnabled ? "开" : "关"}
            tone={settings.textEnabled ? "on" : "off"}
          />
          <SummaryRow
            label="图片处理"
            value={settings.imagesEnabled ? "开" : "关"}
            tone={settings.imagesEnabled ? "on" : "off"}
          />
          <SummaryRow
            label="定价模板"
            value={
              settings.priceTemplateEnabled
                ? settings.template || "开（未选模板）"
                : "关"
            }
            tone={settings.priceTemplateEnabled ? "on" : "off"}
          />
          <SummaryRow
            label="发布到店铺"
            value={
              settings.publishEnabled
                ? settings.publishShopId
                  ? `认领到 ${settings.publishShopId}`
                  : "开（未选店铺）"
                : "关"
            }
            tone={settings.publishEnabled ? "on" : "off"}
          />
          <Separator className="my-0.5" />
          <SummaryRow
            label="目标语言"
            value={settings.textEnabled ? languageLabel(settings.language) : "不翻译"}
            muted={!settings.textEnabled}
          />
          <SummaryRow
            label="尺码图"
            value={settings.sizeChartId ? `已选 #${settings.sizeChartId}` : "未选"}
            muted={!settings.sizeChartId}
          />
        </dl>

        <p className="mt-3 border-t border-border-subtle pt-2 text-2xs leading-4 text-fg-muted">
          批次进度来自当前页面会话；后端没有批次任务表，重开页面会回到「等待开始」。
        </p>
      </Card>

    </div>
  );
}

function Stat({
  label,
  value,
  wide = false,
}: {
  label: string;
  value: string;
  wide?: boolean;
}) {
  return (
    <div className={cn(wide && "col-span-2")}>
      <dt className="text-2xs leading-4 text-fg-muted">{label}</dt>
      <dd className="num mt-0.5 truncate text-[13px] font-medium leading-5 text-fg" title={value}>
        {value}
      </dd>
    </div>
  );
}

function SummaryRow({
  label,
  value,
  tone = "plain",
  muted = false,
}: {
  label: string;
  value: string;
  tone?: "plain" | "on" | "off";
  muted?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="shrink-0 text-xs leading-[18px] text-fg-muted">{label}</dt>
      <dd
        className={cn(
          "min-w-0 truncate text-xs leading-[18px]",
          tone === "on" && "font-medium text-brand-700",
          tone === "off" && "text-fg-muted",
          tone === "plain" && (muted ? "text-fg-muted" : "text-fg")
        )}
        title={value}
      >
        {tone === "on" ? <Badge variant="brand">{value}</Badge> : value}
      </dd>
    </div>
  );
}

export default ProgressPanel;
