// features/automation —— 右侧处理状态与任务摘要
//
// 只展示真实传入的数据，不发起请求、不编造进度。合并为一张紧凑卡片：
// 状态与进度在上，任务摘要在下；所有摘要行固定高度，切换文字/图片开关时
// 只改文字，不改变卡片尺寸。

import { useEffect, useState } from "react";
import { Activity, ListChecks } from "lucide-react";
import { cn, formatCount, formatDuration } from "@/lib/utils";
import { Card, CardTitle, Progress, Separator, StatusBadge } from "@/components/ui";
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
  /** 发布目标店铺名；只拿到 id 时回退显示 id */
  publishShopName?: string;
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
  publishShopName,
  className,
}: ProgressPanelProps) {
  const running = run.phase === "running";
  const elapsed = useTickingElapsed(run.startedAt, running);
  const finalElapsed =
    run.startedAt !== null && run.endedAt !== null
      ? Math.max(0, Math.round((run.endedAt - run.startedAt) / 1000))
      : null;
  const shownElapsed = running ? elapsed : finalElapsed;
  const remaining =
    running && shownElapsed !== null && run.done > 0 && run.total > run.done
      ? Math.round((shownElapsed / run.done) * (run.total - run.done))
      : null;

  const batchPercent = run.total > 0 ? (run.done / run.total) * 100 : 0;
  const imagePercent = run.imagePlanned > 0 ? (run.imageDone / run.imagePlanned) * 100 : 0;
  const textPercent = settings.textEnabled && run.total > 0 ? (run.done / run.total) * 100 : 0;
  const imageTotal = settings.mainImageCount + settings.skuImageCount + settings.detailImageCount;

  const phaseMeta =
    run.phase === "running"
      ? { label: "运行中", tone: "info" as const, dot: "info" as const, breathe: true }
      : run.phase === "done"
        ? { label: "已完成", tone: "success" as const, dot: "success" as const }
        : run.phase === "stopped"
          ? { label: "已停止", tone: "warning" as const, dot: "warning" as const }
          : { label: "等待开始", tone: "neutral" as const, dot: "neutral" as const };

  return (
    <Card padding="normal" className={cn("shrink-0 overflow-hidden", className)}>
      {/* 状态 */}
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5">
          <Activity className="size-4 text-fg-muted" strokeWidth={1.75} />
          <CardTitle>处理状态</CardTitle>
        </span>
        <StatusBadge meta={phaseMeta} />
      </div>

      <div className="mt-3 rounded-md border border-border-subtle bg-canvas-inset/45 p-2.5">
        <div className="flex items-center justify-between gap-3">
          <span className="text-2xs text-fg-muted">当前阶段</span>
          <span className="min-w-0 truncate text-[13px] font-medium text-fg" title={run.stage}>
            {run.stage}
          </span>
        </div>
        <div className="mt-2 grid grid-cols-3 gap-2">
          <Stat label="批次" value={`${formatCount(run.done)} / ${formatCount(run.total)}`} />
          <Stat label="耗时" value={shownElapsed === null ? "—" : formatDuration(shownElapsed)} />
          <Stat label="剩余" value={remaining === null ? "—" : formatDuration(remaining)} />
        </div>
        <Progress className="mt-2" value={batchPercent} label="批次进度" showValue />
      </div>

      {/* 两条真实进度：文字按件，图片按实际提交张数 */}
      <div className="mt-3 grid gap-2.5">
        <div>
          <Progress
            value={textPercent}
            tone={settings.textEnabled ? "brand" : "warning"}
            label="商品设置与文字"
            showValue
          />
          <p className="num mt-1 h-4 truncate text-2xs leading-4 text-fg-muted">
            {settings.textEnabled
              ? `${formatCount(run.done)} / ${formatCount(run.total)} 件 · ${languageLabel(settings.language)}`
              : "已关闭 · 不翻译标题与属性"}
          </p>
        </div>
        <div>
          <Progress
            value={imagePercent}
            tone={settings.imagesEnabled ? "brand" : "warning"}
            label="图片处理"
            showValue
          />
          <p className="num mt-1 h-4 truncate text-2xs leading-4 text-fg-muted">
            {settings.imagesEnabled
              ? `${formatCount(run.imageDone)} / ${formatCount(run.imagePlanned)} 张 · 每件 ${imageTotal} 张`
              : "已关闭 · 不处理图片"}
          </p>
        </div>
      </div>

      <Separator className="my-3" />

      {/* 摘要：固定 2×4 网格，开关变化不会让框长高 */}
      <div className="flex items-center gap-1.5">
        <ListChecks className="size-4 text-fg-muted" strokeWidth={1.75} />
        <h3 className="text-[13px] font-semibold leading-5 text-fg">任务摘要</h3>
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-2">
        <SummaryStat label="已选" value={`${formatCount(selectedCount)} 件`} strong />
        <SummaryStat label="本页" value={`${formatCount(visibleCount)} 件`} />
        <SummaryStat label="文字" value={settings.textEnabled ? "开" : "关"} active={settings.textEnabled} />
        <SummaryStat label="图片" value={settings.imagesEnabled ? "开" : "关"} active={settings.imagesEnabled} />
        <SummaryStat
          label="定价"
          value={settings.priceTemplateEnabled ? settings.template || "未选模板" : "关"}
          active={settings.priceTemplateEnabled}
        />
        <SummaryStat
          label="发布"
          value={settings.publishEnabled ? publishShopName || settings.publishShopId || "未选店铺" : "关"}
          active={settings.publishEnabled}
        />
        <SummaryStat
          label="语言"
          value={settings.textEnabled ? languageLabel(settings.language) : "不翻译"}
        />
        <SummaryStat label="尺码图" value={settings.sizeChartId ? "已选" : "未选"} />
      </dl>

      <p className="mt-3 text-2xs leading-4 text-fg-muted">
        运行记录已保存；待处理项目可继续，状态不确定的项目需先核对。
      </p>

      {(run.needsReview ?? 0) > 0 && (
        <p className="mt-2 rounded-md border border-warning-600/20 bg-warning-50 px-2.5 py-1.5 text-2xs leading-4 text-warning-700">
          {formatCount(run.needsReview ?? 0)} 项状态待核对，不会自动重跑
        </p>
      )}

      {run.failed > 0 && (
        <div className="mt-3 border-t border-border-subtle pt-3">
          <p className="rounded-md border border-warning-600/20 bg-warning-50 px-2.5 py-1.5 text-2xs leading-4 text-warning-700">
            本批失败 {formatCount(run.failed)} 件
          </p>
          {run.failures.length > 0 && (
            <ul className="mt-2 flex max-h-36 flex-col gap-1 overflow-y-auto">
              {run.failures.map((failure) => (
                <li
                  key={failure.key}
                  className="rounded border border-warning-600/20 bg-warning-50/60 px-2 py-1.5"
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
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs leading-4 text-fg-muted">{label}</dt>
      <dd className="num mt-0.5 h-5 truncate text-[13px] font-medium leading-5 text-fg" title={value}>
        {value}
      </dd>
    </div>
  );
}

function SummaryStat({
  label,
  value,
  active = false,
  strong = false,
}: {
  label: string;
  value: string;
  active?: boolean;
  strong?: boolean;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs leading-4 text-fg-muted">{label}</dt>
      <dd
        className={cn(
          "num mt-0.5 h-5 truncate text-[13px] leading-5",
          active ? "font-medium text-brand-700" : strong ? "font-semibold text-fg" : "text-fg"
        )}
        title={value}
      >
        {value}
      </dd>
    </div>
  );
}

export default ProgressPanel;
