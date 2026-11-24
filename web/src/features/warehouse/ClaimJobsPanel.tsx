import { useState } from "react";
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { api, apiErrorMessage } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { formatDate } from "@/lib/utils";
import type {
  BatchDetailResponse,
  BatchListResponse,
  PublishJobRetryResponse,
} from "@/lib/types";
import { Badge, Button, Card, ErrorState, Progress } from "@/components/ui";
import { useRefreshPublishLogs } from "@/lib/queries";
import { toast } from "@/components/ui/toast";

const labels: Record<string, string> = {
  pending: "待开始", running: "进行中", done: "已完成", failed: "失败",
  ok: "已导入采集箱", skipped: "已跳过", stopped: "已停止", paused: "已暂停",
  processing: "等待平台确认",
};

/** 发布任务的逐件状态用发布口径说人话：ok 不是「已导入采集箱」。 */
const publishLabels: Record<string, string> = {
  pending: "待发布", running: "正在发布", ok: "已提交",
  failed: "发布失败", skipped: "已跳过",
};

const stageLabels: Record<string, string> = {
  saving: "\u6b63\u5728\u5199\u5165\u5546\u54c1",
  saved: "\u5546\u54c1\u5185\u5bb9\u5df2\u4fdd\u5b58\uff0c\u6b63\u5728\u786e\u8ba4\u91c7\u96c6\u7bb1",
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
  const refreshLogs = useRefreshPublishLogs();
  const queryClient = useQueryClient();

  // 删除任务记录：只删本地批次台账，妙手侧已发出的商品不受影响。
  const removeJob = useMutation({
    mutationFn: (batchId: string) =>
      api.del<{ deleted: number }>(
        "/claim-jobs/" + encodeURIComponent(batchId),
        undefined,
        { query: { accountId } },
      ),
    onSuccess: () => {
      toast.success("任务记录已删除");
      void queryClient.invalidateQueries({ queryKey: queryKeys.claimJobs.all });
      void list.refetch();
    },
    onError: (error: Error) => {
      toast.error("删除失败", { description: apiErrorMessage(error) });
    },
  });

  /**
   * 明细里哪些行不该显示（负责人 2026-11-23）。
   *
   * 认领/处理任务的逐件记录，在商品还没确定落到哪个店铺时 detailId 只是
   * 仓库侧的数字 ID（例如 `520`）。这些行展示出来就是「仓库商品 520 · 进行中」
   * 一长串 —— 既看不出是哪件商品，也没有可操作的信息，只是把列表撑长。
   * 真正的店铺商品 ID 是「店铺:商品」形式，那种才显示。
   *
   * 注意只过滤**展示**：任务进度和统计仍然按全部条目算，
   * 否则「已处理 4/9」会和明细条数对不上，看着像丢数据。
   */
  const isDisplayableItem = (item: { detailId: string | null; status: string }): boolean => {
    const id = String(item.detailId ?? "").trim();
    if (!id) return false;
    // 纯数字 = 还没绑定到店铺的仓库侧 ID
    if (/^\d+$/.test(id)) return false;
    return true;
  };

  // 发布任务的 detailId 是「店铺:商品」，展示时拆开，让人看得出发到哪个店。
  const renderDetailLabel = (detailId: string): string => {
    const separator = detailId.indexOf(":");
    if (separator <= 0) return "仓库商品 " + detailId;
    return "店铺 " + detailId.slice(0, separator) + " · 商品 "
      + detailId.slice(separator + 1);
  };

  // 单店重试：只重跑失败的店铺 / 失败的商品，已成功的一件都不碰。
  //
  // 为什么不能「整批重发」（负责人 2026-11-19）：发布不可撤销。
  // 5 个店发 10 件、1 个店失败时整批重发，会把另外 4 个店已经发出去的
  // 40 件再发一遍，等于凭空多建 40 件商品。
  const retryJob = useMutation({
    mutationFn: (batchId: string) =>
      api.post<PublishJobRetryResponse>(
        `/publish-jobs/${encodeURIComponent(batchId)}/retry`,
        {},
        { query: { accountId }, timeoutMs: 30_000 },
      ),
    onSuccess: (data) => {
      toast.success(data.message || "已提交重试", {
        description: data.shops.length > 0
          ? `店铺 ${data.shops.join("、")} 正在后台重发，进度看下方进度条`
          : undefined,
        duration: 8000,
      });
      void list.refetch();
      void detail.refetch();
    },
    onError: (error: Error) => {
      toast.error("重试失败", { description: apiErrorMessage(error) });
    },
  });
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
          loading={list.isFetching || refreshLogs.isPending}
          disabled={!accountId}
          onClick={() => {
            void list.refetch();
            if (expanded) void detail.refetch();
            // 有 processing 发布批次时，同时刷新平台回执
            const hasProcessing = list.data?.items.some(
              (job) => job.status === "processing" && job.id.startsWith("pub_")
            );
            if (hasProcessing) {
              refreshLogs.mutate(undefined, {
                onSuccess: (data) => {
                  if (data.updated > 0) {
                    toast.success(`已更新 ${data.updated} 条发布回执`);
                    void list.refetch();
                  }
                },
                onError: () => {/* 静默失败，不干扰刷新 */},
              });
            }
          }}
        >刷新任务</Button>
      </div>
      {!accountId && <p className="text-sm text-fg-muted">连接妙手账号后查看任务。</p>}
      {list.isLoading && accountId && <p className="text-sm text-fg-muted">正在加载任务…</p>}
      {list.isError && <ErrorState title="任务加载失败" description={apiErrorMessage(list.error)} onRetry={() => void list.refetch()} />}
      {list.data?.items.length === 0 && <p className="text-sm text-fg-muted">当前妙手账号暂无认领任务。选中仓库商品后，点击认领即可在这里查看进度。</p>}
      {list.data?.items.map((job) => {
        const total = job.detailIds.length;
        const active = job.status === "pending" || job.status === "running";
        const progress = job.settings.progress;
        // 发布任务和认领任务是两套口径，混着算会得出「已处理 50/10」这种数字：
        //   · 认领按「组」推进（offset + processed）；
        //   · 发布按「店铺 × 商品」推进，currentIndex 是店铺序号，不是件数。
        const isPublishJob = job.settings.kind === "publish_job";
        const publishAttempted = isPublishJob
          ? (progress?.attempted ?? total)
          : total;
        const progressDone = isPublishJob
          ? job.results.length
          : active && typeof progress?.processed === "number"
            ? (progress.offset ?? 0) + progress.processed
            : job.currentIndex;
        const done = Math.min(publishAttempted, progressDone);
        const completedSuccesses = job.results.filter((row) => row.status === "ok").length;
        const completedFailures = job.results.filter((row) => row.status === "failed").length;
        const isPublishProcessing = job.status === "processing" && job.id.startsWith("pub_");
        // 发布已提交等待平台回执时，所有 processing 结果都算"已提交"
        const submittedCount = isPublishProcessing
          ? job.results.filter((row) => row.status === "processing").length
          : 0;
        const succeeded = completedSuccesses + (active ? progress?.verified ?? 0 : 0);
        const failed = completedFailures + (active ? progress?.failed ?? 0 : 0);
        const pending = Math.max(0, publishAttempted - done);
        const failureMessage = job.results.find((row) => row.status === "failed")?.message;
        // 有失败件且任务已结束 → 可以只重试失败的那部分。
        // 在跑的不给按钮（后端也会 409），全部成功的不给按钮（后端会 400）。
        const canRetry = !active && completedFailures > 0;
        const failedShopIds = Array.from(new Set(
          job.results
            .filter((row) => row.status === "failed")
            .map((row) => row.shopId || String(row.detailId).split(":")[0])
            .filter(Boolean),
        ));
        const shopLabel = isPublishJob
          ? `店铺 ${failedShopIds.length > 0 ? failedShopIds.join("、") : job.shopId}`
          : `店铺 ${job.shopId}`;
        return (
          <Card key={job.id} className="p-4">
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <Badge variant={job.status === "done" ? "success" : job.status === "failed" ? "danger" : job.status === "processing" ? "warning" : "info"}>{job.status === "failed" && succeeded > 0 ? "部分失败" : labels[job.status] ?? job.status}</Badge>
              <span>{shopLabel}</span>
              <span className="text-fg-muted">{formatDate(job.createdAt)}</span>
              <div className="ml-auto flex items-center gap-1">
                {/* 单店重试：只重跑失败的店铺 / 失败的商品，已成功的一件都不碰。
                    整批重发会把已经发成功的店铺再发一遍（发布不可撤销）。 */}
                {canRetry && isPublishJob && (
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={retryJob.isPending && retryJob.variables === job.id}
                    onClick={() => retryJob.mutate(job.id)}
                  >重试失败店铺</Button>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  aria-expanded={expanded === job.id}
                  aria-controls={`claim-job-${job.id}`}
                  onClick={() => setExpanded(expanded === job.id ? null : job.id)}
                >{expanded === job.id ? "收起明细" : "查看明细"}</Button>
                {/* 只有已结束的任务能删：在跑的任务删掉会让后台线程写到一半没行可写。 */}
                {!active && (
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-danger-600 hover:text-danger-700"
                    loading={removeJob.isPending && removeJob.variables === job.id}
                    onClick={() => {
                      if (window.confirm("删除这条任务记录？（只删本地台账，妙手侧商品不受影响）")) {
                        removeJob.mutate(job.id);
                      }
                    }}
                  >删除</Button>
                )}
              </div>
            </div>
            <Progress
              className="mt-3"
              label={isPublishJob
                ? `已处理 ${done} / ${publishAttempted} 件`
                : `已处理 ${done} / ${total} 件`}
              value={publishAttempted ? Math.round(done * 100 / publishAttempted) : 0}
              tone={job.status === "failed" ? "danger" : job.status === "done" ? "success" : "brand"}
              showValue
            />
            <p className="mt-2 text-sm text-fg-secondary" role="status">
              {isPublishProcessing
                ? <>已提交 <span className="num">{submittedCount}</span> 件，等待平台回执（点「刷新任务」获取最新结果）</>
                : <>成功 <span className="num">{succeeded}</span> 件 · 失败 <span className="num">{failed}</span> 件 · {active ? "待处理" : "未处理"} <span className="num">{pending}</span> 件</>
              }
            </p>
            {active && isPublishJob && (
              <p className="mt-2 text-sm text-fg-muted" role="status">
                正在发布第 {Math.max(1, progress?.shopIndex ?? 0)} / {progress?.shopTotal ?? 1} 个店铺
                {typeof progress?.attempted === "number" && ` · 本轮 ${progress.attempted} 件`}
                {typeof progress?.succeeded === "number" && ` · 成功 ${progress.succeeded} 件`}
                {typeof progress?.failed === "number" && progress.failed > 0 && ` · 失败 ${progress.failed} 件`}
                。每个店铺都要在妙手重建载体，单店约 30~60 秒。
              </p>
            )}
            {active && !isPublishJob && progress ? (
              <p className="mt-2 text-sm text-fg-muted" role="status">
                {stageLabels[progress.stage] ?? "正在处理当前组"}
                {(progress.groupSize ?? 0) > 0 && `（第 ${(progress.offset ?? 0) + 1}–${Math.min(total, (progress.offset ?? 0) + (progress.groupSize ?? 0))} 件）`}
                {typeof progress.processed === "number" && ` · 本组已处理 ${progress.processed}/${progress.groupSize}`}
                {typeof progress.verified === "number" && ` · 成功 ${progress.verified} 件`}
                {typeof progress.failed === "number" && ` · 失败 ${progress.failed} 件`}
                {progress.warehouseId && ` · 当前商品 ${progress.warehouseId}`}
                。
              </p>
            ) : active && done === 0 && !isPublishJob && <p className="mt-2 text-sm text-fg-muted">正在准备并导入商品，进度会在每批完成后更新。</p>}
            {job.status === "failed" && (
              <p className="mt-2 break-words text-sm text-danger-700">
                {failureMessage || "任务未全部完成，请展开明细查看结果。"}
                {isPublishJob
                  ? " 点「重试失败店铺」只重发失败的店铺和商品，已发成功的不会重发。"
                  : " 请先核对采集箱，确认后再处理未成功的商品。"}
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
                {detail.data?.items.filter(isDisplayableItem).map((item) => (
                  <div key={item.id} className="flex flex-wrap gap-x-3 gap-y-1 border-t border-border-subtle py-2">
                    <span className="min-w-0 flex-1 break-all">{renderDetailLabel(item.detailId)}</span>
                    <span className="shrink-0">
                      {isPublishJob
                        ? (publishLabels[item.status] ?? item.status)
                        : (labels[item.status] ?? item.status)}
                      {item.attempt > 1 && (
                        <span className="ml-1 text-fg-muted">（第 {item.attempt} 次）</span>
                      )}
                    </span>
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
