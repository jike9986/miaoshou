// features/automation — 执行历史
//
// 按时间倒序列出每一批：跑了几件、成几件、挂几件、耗时。点开看明细。
// 只区分两种结果：失败 / 已完成。失败再分「图片翻译失败」和「文字处理失败」。

import { useMemo, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, History, X } from "lucide-react";
import { cn, formatCount, formatDuration } from "@/lib/utils";
import { Card, CardTitle, StatusBadge } from "@/components/ui";
import { useAutomationRunHistory, useDeleteAutomationRun } from "@/lib/queries";
import type { AutomationRunIssue } from "@/lib/queries";
import { languageLabel } from "@/features/automation/types";

export interface RunHistoryPanelProps {
  accountId: string | null;
  shopId: string | null;
  running?: boolean;
  className?: string;
}

/** 判断 issue 是图片翻译失败还是文字处理失败 */
function issueCategoryLabel(issue: AutomationRunIssue): string {
  const r = (issue.reason || "").toLowerCase();
  if (
    r.includes("图片") ||
    r.includes("image") ||
    r.includes("翻译未通过") ||
    r.includes("超时") ||
    r.includes("timeout") ||
    r.includes("502") ||
    r.includes("bad gateway")
  ) {
    return "图片翻译失败";
  }
  return "文字处理失败";
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
        <p className="mt-3 text-2xs text-fg-muted">暂无执行记录。</p>
      ) : (
        <ul className="mt-2.5 flex flex-col gap-1.5 overflow-y-auto pr-1" style={{ maxHeight: "16rem" }}>
          {runs.map((run) => {
            const expanded = openId === run.id;
            const hasIssue = run.failed > 0 || run.needsReview > 0;
            const elapsed =
              run.finishedAt !== null && run.createdAt
                ? Math.max(0, Math.round(run.finishedAt - run.createdAt))
                : null;
            // 只区分两种：失败 / 已完成
            const meta =
              run.status === "running"
                ? { label: "运行中", tone: "info" as const, dot: "info" as const }
                : hasIssue
                  ? { label: "失败", tone: "warning" as const, dot: "warning" as const }
                  : { label: "已完成", tone: "success" as const, dot: "success" as const };
            return (
              <li key={run.id} className="rounded-md border border-border-subtle bg-canvas-inset/40">
                {/* 折叠行 + × 删除按钮同一行 */}
                <div className="flex items-center">
                  <button
                    type="button"
                    onClick={() => setOpenId(expanded ? null : run.id)}
                    className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left"
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
                  <button
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation();
                      if (removeRun.isPending) return;
                      removeRun.mutate(run.id, { onSuccess: () => setOpenId(null) });
                    }}
                    disabled={removeRun.isPending || run.status === "running"}
                    aria-label="删除这条执行历史"
                    title={run.status === "running" ? "运行中不能删除" : "删除这条执行历史"}
                    className={cn(
                      "mr-1.5 grid size-6 shrink-0 place-items-center rounded-sm text-fg-muted",
                      "transition-colors hover:bg-danger-50 hover:text-danger-600",
                      "disabled:pointer-events-none disabled:opacity-40"
                    )}
                  >
                    <X className="size-3" strokeWidth={2.4} />
                  </button>
                </div>
                {expanded && (
                  <div className="border-t border-border-subtle px-2 py-2">
                    <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5">
                      <HistStat label="耗时" value={elapsed === null ? "—" : formatDuration(elapsed)} />
                      <HistStat label="语言" value={run.textEnabled ? languageLabel(run.language) : "不翻译"} />
                      <HistStat label="图片" value={run.imagesEnabled ? "开" : "关"} />
                      <HistStat label="发布" value={run.publishEnabled ? "开" : "关"} />
                    </dl>

                    {/* 逐件异常明细：只显示图片翻译失败 / 文字处理失败两类 */}
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
                              className="rounded-sm bg-surface/80 px-1.5 py-1"
                            >
                              <p
                                className="num truncate text-2xs font-medium text-fg"
                                title={issue.title || issue.detailId}
                              >
                                {issue.detailId || issue.productId}
                                {issue.title ? ` · ${issue.title}` : ""}
                              </p>
                              <p className="mt-0.5 text-2xs leading-4 text-warning-700">
                                {issueCategoryLabel(issue)}
                                {issue.reason ? `：${issue.reason}` : ""}
                              </p>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
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

function formatTimestamp(value: number): string {
  if (!value) return "—";
  const date = new Date(value * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export default RunHistoryPanel;
