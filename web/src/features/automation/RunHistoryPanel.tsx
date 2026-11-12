// features/automation —— 执行历史
//
// 为什么单独做一块：右侧「处理状态」是**当前这一批**的实时进度，不该承担
// 历史错误清单。之前每次打开页面，右下角都堆着上一次的失败原因，运营第一眼
// 看到的是「上次怎么坏的」而不是「这次跑到哪」，也分不清哪些是历史
// （负责人 2026-11-10 要求分离）。
//
// 这里按时间倒序列出每一批：跑了几件、成几件、挂几件、耗时。点开看明细。

import { useMemo, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, History, X } from "lucide-react";
import { cn, formatCount, formatDuration } from "@/lib/utils";
import { Card, CardTitle, StatusBadge } from "@/components/ui";
import { useAutomationRunHistory, useDeleteAutomationRun } from "@/lib/queries";
import { languageLabel } from "@/features/automation/types";

export interface RunHistoryPanelProps {
  accountId: string | null;
  shopId: string | null;
  /** 运行中时不要频繁刷新历史，避免抢带宽 */
  running?: boolean;
  className?: string;
}

export function RunHistoryPanel({ accountId, shopId, running = false, className }: RunHistoryPanelProps) {
  const history = useAutomationRunHistory(accountId, shopId, true, 30);
  const [openId, setOpenId] = useState<string | null>(null);
  const removeRun = useDeleteAutomationRun();
  const runs = useMemo(() => history.data?.runs ?? [], [history.data]);

  return (
    <Card padding="normal" className={cn("shrink-0", className)}>
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5">
          <History className="size-4 text-fg-muted" strokeWidth={1.75} />
          <CardTitle>执行历史</CardTitle>
        </span>
        <span className="num text-2xs text-fg-muted">
          {running ? "运行中·暂停刷新" : `${formatCount(runs.length)} 批`}
        </span>
      </div>

      {history.isLoading ? (
        <p className="mt-3 text-2xs text-fg-muted">正在读取…</p>
      ) : runs.length === 0 ? (
        <p className="mt-3 text-2xs text-fg-muted">还没有执行记录。开始处理后会在这里留档。</p>
      ) : (
        <ul className="mt-2.5 flex max-h-72 flex-col gap-1.5 overflow-y-auto pr-1">
          {runs.map((run) => {
            const expanded = openId === run.id;
            const hasIssue = run.failed > 0 || run.needsReview > 0;
            const elapsed =
              run.finishedAt !== null && run.createdAt
                ? Math.max(0, Math.round(run.finishedAt - run.createdAt))
                : null;
            const meta = hasIssue
              ? { label: "需检查", tone: "warning" as const, dot: "warning" as const }
              : run.status === "running"
                ? { label: "运行中", tone: "info" as const, dot: "info" as const }
                : { label: "已完成", tone: "success" as const, dot: "success" as const };
            return (
              <li key={run.id} className="rounded-md border border-border-subtle bg-canvas-inset/40">
                <button
                  type="button"
                  onClick={() => setOpenId(expanded ? null : run.id)}
                  className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
                  aria-expanded={expanded}
                >
                  {expanded ? (
                    <ChevronDown className="size-3.5 shrink-0 text-fg-muted" />
                  ) : (
                    <ChevronRight className="size-3.5 shrink-0 text-fg-muted" />
                  )}
                  <span className="num min-w-0 flex-1 truncate text-2xs text-fg">
                    {formatTimestamp(run.createdAt)}
                  </span>
                  <span className="num shrink-0 text-2xs text-fg-muted">
                    {formatCount(run.succeeded)}/{formatCount(run.total)}
                    {hasIssue ? ` · 挂 ${formatCount(run.failed + run.needsReview)}` : ""}
                  </span>
                  <StatusBadge meta={meta} />
                </button>
                {expanded && (
                  <div className="border-t border-border-subtle px-2 py-2">
                    <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5">
                    <HistStat label="耗时" value={elapsed === null ? "—" : formatDuration(elapsed)} />
                    <HistStat label="失败" value={`${formatCount(run.failed)} 件`} />
                    <HistStat label="待核对" value={`${formatCount(run.needsReview)} 项`} />
                    <HistStat label="语言" value={run.textEnabled ? languageLabel(run.language) : "不翻译"} />
                    <HistStat label="图片" value={run.imagesEnabled ? "开" : "关"} />
                    <HistStat label="发布" value={run.publishEnabled ? "开" : "关"} />
                    <HistStat label="定价" value={run.template || "关"} />
                    <HistStat label="批次号" value={run.id.slice(-8)} />
                    </dl>

                    {/* 逐件异常明细：负责人口径 2026-11-12「要检查到底要检查什么，
                        哪里有问题就写出来不要哪里有问题都不清楚」。
                        之前只给「待核对 2 项」这个数字，点开也看不到是哪两件、
                        为什么挂 —— 现在把商品号 + 阶段 + 具体原因逐条列出来。 */}
                    {(run.issues?.length ?? 0) > 0 && (
                      <div className="mt-2.5 rounded-sm border border-warning-100 bg-warning-50/60 p-2">
                        <p className="flex items-center gap-1 text-2xs font-medium text-warning-700">
                          <AlertTriangle className="size-3 shrink-0" strokeWidth={2} />
                          有问题的 {run.issues!.length} 件
                        </p>
                        <ul className="mt-1.5 flex flex-col gap-1.5">
                          {run.issues!.map((issue) => (
                            <li
                              key={`${issue.detailId}-${issue.stage}`}
                              className="rounded-xs bg-surface/80 px-1.5 py-1"
                            >
                              <p className="num truncate text-2xs font-medium text-fg" title={issue.title || issue.detailId}>
                                {issue.detailId || issue.productId}
                                {issue.title ? ` · ${issue.title}` : ""}
                              </p>
                              <p className="mt-0.5 text-2xs leading-4 text-warning-700">
                                {issue.stage === "publish" ? "发布阶段：" : "处理阶段："}
                                {issue.reason}
                              </p>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* 删除这条历史。只删记录，不动已入库商品和妙手草稿。 */}
                    <div className="mt-2 flex justify-end">
                      <button
                        type="button"
                        onClick={() => {
                          if (removeRun.isPending) return;
                          removeRun.mutate(run.id, {
                            onSuccess: () => setOpenId(null),
                          });
                        }}
                        disabled={removeRun.isPending || run.status === "running"}
                        className={cn(
                          "inline-flex items-center gap-1 rounded-sm px-1.5 py-1 text-2xs font-medium",
                          "text-fg-muted transition-colors hover:bg-danger-50 hover:text-danger-600",
                          "disabled:pointer-events-none disabled:opacity-50"
                        )}
                        title={run.status === "running" ? "运行中不能删除" : "删除这条执行历史"}
                      >
                        <X className="size-3" strokeWidth={2.4} />
                        删除这条记录
                      </button>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function HistStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs leading-4 text-fg-muted">{label}</dt>
      <dd className="num mt-0.5 truncate text-2xs leading-4 text-fg" title={value}>
        {value}
      </dd>
    </div>
  );
}

/** 秒级时间戳 -> MM-DD HH:mm（历史列表只要到这个粒度） */
function formatTimestamp(value: number): string {
  if (!value) return "—";
  const date = new Date(value * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export default RunHistoryPanel;
