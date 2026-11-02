import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiErrorMessage } from "@/lib/api";
import type { BatchDetailResponse, BatchListResponse } from "@/lib/types";
import { Badge, Button, Card, ErrorState, Progress } from "@/components/ui";

const labels: Record<string, string> = {
  pending: "待开始", running: "进行中", done: "已完成", failed: "失败",
  ok: "成功", skipped: "已跳过",
};

export function ClaimJobsPanel({ accountId }: { accountId: string | null }) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState<string | null>(null);
  const list = useQuery({
    queryKey: ["claim-jobs", accountId],
    queryFn: () => api.get<BatchListResponse>("/claim-jobs", { query: { accountId } }),
    enabled: Boolean(accountId),
    refetchInterval: (query) => query.state.data?.items.some(
      (job) => job.status === "pending" || job.status === "running"
    ) ? 3000 : false,
  });
  const detail = useQuery({
    queryKey: ["claim-jobs", accountId, expanded],
    queryFn: () => api.get<BatchDetailResponse>(`/claim-jobs/${encodeURIComponent(expanded!)}`, { query: { accountId } }),
    enabled: Boolean(accountId && expanded),
    refetchInterval: (query) => ["pending", "running"].includes(query.state.data?.batch.status ?? "") ? 3000 : false,
  });
  const progress = list.data?.items.map((job) => `${job.id}:${job.currentIndex}:${job.status}`).join("|");
  useEffect(() => {
    if (progress !== undefined) void queryClient.invalidateQueries({ queryKey: ["warehouse"] });
  }, [progress, queryClient]);
  useEffect(() => setExpanded(null), [accountId]);

  return (
    <section className="mb-5 space-y-3">
      <h2 className="text-sm font-medium text-fg">采集到采集箱任务</h2>
      {!accountId && <p className="text-sm text-fg-muted">连接妙手账号后查看任务。</p>}
      {list.isLoading && accountId && <p className="text-sm text-fg-muted">正在加载任务…</p>}
      {list.isError && <ErrorState title="任务加载失败" description={apiErrorMessage(list.error)} onRetry={() => void list.refetch()} />}
      {list.data?.items.length === 0 && <p className="text-sm text-fg-muted">暂无采集任务</p>}
      {list.data?.items.map((job) => {
        const total = job.detailIds.length;
        const done = job.currentIndex;
        const failed = job.results.filter((row) => row.status === "failed").length;
        return (
          <Card key={job.id} className="p-4">
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <Badge variant={job.status === "done" ? "success" : job.status === "failed" ? "danger" : "info"}>{labels[job.status] ?? job.status}</Badge>
              <span>店铺 {job.shopId}</span>
              <span>{done} / {total} 件，失败 {failed} 件</span>
              <Button size="sm" variant="ghost" className="ml-auto" onClick={() => setExpanded(expanded === job.id ? null : job.id)}>{expanded === job.id ? "收起明细" : "查看明细"}</Button>
            </div>
            <Progress className="mt-3" value={total ? Math.round(done * 100 / total) : 0} />
            {expanded === job.id && (
              <div className="mt-3 max-h-64 overflow-auto text-xs">
                {detail.isLoading && <p>正在加载明细…</p>}
                {detail.isError && <p className="text-danger-700">{apiErrorMessage(detail.error)}</p>}
                {detail.data?.items.map((item) => (
                  <div key={item.id} className="flex gap-3 border-t border-border-subtle py-2">
                    <span className="min-w-0 flex-1 break-all">{item.detailId}</span>
                    <span>{labels[item.status] ?? item.status}</span>
                    {item.message && <span className="text-fg-muted">{item.message}</span>}
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
