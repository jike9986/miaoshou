import { useState } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { api, apiErrorMessage } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { formatDate } from "@/lib/utils";
import type { BatchDetailResponse, BatchListResponse } from "@/lib/types";
import { Badge, Button, Card, ErrorState, Progress } from "@/components/ui";

const labels: Record<string, string> = {
  pending: "待开始", running: "进行中", done: "已完成", failed: "失败",
  ok: "已导入采集箱", skipped: "已跳过", stopped: "已停止", paused: "已暂停",
};

const stageLabels: Record<string, string> = {
  copying: "正在批量复制采集箱载体",
  reuse: "正在准备可复用的采集箱载体",
  collect: "正在获取采集箱载体",
  collected: "载体已采集，等待上游确认",
  waiting: "正在等待载体采集完成",
  link_failed: "货源链接采集失败",
  collect_failed: "载体采集未完成",
  claim: "正在准备商品导入",
  warehouse: "正在准备目标店铺商品仓",
  fill: "正在写入并读回核验商品",
  verified: "当前组已完成读回核验",
  verify_failed: "当前组有商品未通过读回核验",
};

export function ClaimJobsPanel({ accountId, operatorId, list, focusJobId }: {
  accountId: string | null;
  operatorId: string | null;
  list: UseQueryResult<BatchListResponse>;
  focusJobId: string | null;
}) {
  const [expanded, setExpanded] = useState<string | null>(focusJobId);
  const detail = useQuery({
    queryKey: queryKeys.claimJobs.detail(operatorId, accountId, expanded ?? "none"),
    queryFn: () => api.get<BatchDetailResponse>(`/claim-jobs/${encodeURIComponent(expanded!)}`, { query: { accountId } }),
    enabled: Boolean(accountId && expanded),
    refetchOnMount: "always",
    refetchInterval: (query) => ["pending", "running"].includes(query.state.data?.batch.status ?? "") ? 3000 : false,
  });

  return (
    <section className="mb-5 mt-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-fg">认领到采集箱</h2>
        <Button
          size="sm"
          variant="secondary"
          icon={<RefreshCw className="size-3.5" />}
          loading={list.isFetching}
          disabled={!accountId}
          onClick={() => {
            void list.refetch();
            if (expanded) void detail.refetch();
          }}
        >刷新任务</Button>
      </div>
      {!accountId && <p className="text-sm text-fg-muted">连接妙手账号后查看任务。</p>}
      {list.isLoading && accountId && <p className="text-sm text-fg-muted">正在加载任务…</p>}
      {list.isError && <ErrorState title="任务加载失败" description={apiErrorMessage(list.error)} onRetry={() => void list.refetch()} />}
      {list.data?.items.length === 0 && <p className="text-sm text-fg-muted">当前妙手账号暂无认领任务。选中仓库商品后，点击认领即可在这里查看进度。</p>}
      {list.data?.items.map((job) => {
        const total = job.detailIds.length;
        const done = Math.min(total, job.currentIndex);
        const succeeded = job.results.filter((row) => row.status === "ok").length;
        const failed = job.results.filter((row) => row.status === "failed").length;
        const pending = Math.max(0, total - done);
        const active = job.status === "pending" || job.status === "running";
        const failureMessage = job.results.find((row) => row.status === "failed")?.message;
        const progress = job.settings.progress;
        return (
          <Card key={job.id} className="p-4">
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <Badge variant={job.status === "done" ? "success" : job.status === "failed" ? "danger" : "info"}>{job.status === "failed" && succeeded > 0 ? "部分失败" : labels[job.status] ?? job.status}</Badge>
              <span>店铺 {job.shopId}</span>
              <span className="text-fg-muted">{formatDate(job.createdAt)}</span>
              <Button
                size="sm"
                variant="ghost"
                className="ml-auto"
                aria-expanded={expanded === job.id}
                aria-controls={`claim-job-${job.id}`}
                onClick={() => setExpanded(expanded === job.id ? null : job.id)}
              >{expanded === job.id ? "收起明细" : "查看明细"}</Button>
            </div>
            <Progress
              className="mt-3"
              label={`已处理 ${done} / ${total} 件`}
              value={total ? Math.round(done * 100 / total) : 0}
              tone={job.status === "failed" ? "danger" : job.status === "done" ? "success" : "brand"}
              showValue
            />
            <p className="mt-2 text-sm text-fg-secondary" role="status">
              成功 <span className="num">{succeeded}</span> 件 · 失败 <span className="num">{failed}</span> 件 · {active ? "待处理" : "未处理"} <span className="num">{pending}</span> 件
            </p>
            {active && progress ? (
              <p className="mt-2 text-sm text-fg-muted" role="status">
                {stageLabels[progress.stage] ?? "正在处理当前组"}
                {progress.groupSize > 0 && `（第 ${progress.offset + 1}–${Math.min(total, progress.offset + progress.groupSize)} 件）`}
                {typeof progress.verified === "number" && ` · 本组读回核验 ${progress.verified} 件`}
                。最终导入结果以成功、失败明细为准。
              </p>
            ) : active && done === 0 && <p className="mt-2 text-sm text-fg-muted">正在准备并导入商品，进度会在每批完成后更新。</p>}
            {job.status === "failed" && (
              <p className="mt-2 break-words text-sm text-danger-700">
                {failureMessage || "任务未全部完成，请展开明细查看结果。"} 请先核对采集箱，确认后再处理未成功的商品。
              </p>
            )}
            {expanded === job.id && (
              <div id={`claim-job-${job.id}`} className="mt-3 max-h-64 overflow-auto text-sm">
                {detail.isLoading && <p>正在加载明细…</p>}
                {detail.isError && (
                  <div className="flex items-center gap-2 text-danger-700" role="alert">
                    <p>{apiErrorMessage(detail.error)}</p>
                    <Button size="sm" variant="ghost" onClick={() => void detail.refetch()}>重试明细</Button>
                  </div>
                )}
                {detail.data?.items.map((item) => (
                  <div key={item.id} className="flex flex-wrap gap-x-3 gap-y-1 border-t border-border-subtle py-2">
                    <span className="min-w-0 flex-1 break-all">仓库商品 {item.detailId}</span>
                    <span className="shrink-0">{labels[item.status] ?? item.status}</span>
                    {item.message && <span className="w-full break-words text-fg-muted">{item.message}</span>}
                  </div>
                ))}
              </div>
            )}
          </Card>
        );
      })}
    </section>
  );
}
