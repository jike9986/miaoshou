// features/publish —— 发布记录（api.md §8.2 GET /publish/logs + POST /publish/logs/refresh）
// 列表：商品 / 店铺 / 状态 / 时间 / 消息；顶部「刷新回执」回源平台更新 processing 记录。

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ExternalLink, RefreshCw, Search, Undo2 } from "lucide-react";
import { cn, formatCount, formatRelative } from "@/lib/utils";
import { ApiError, apiErrorMessage } from "@/lib/api";
import { publishStatus } from "@/features/shared/status";
import { usePublishLogs, useRefreshPublishLogs } from "@/lib/queries";
import { usePublishLogsStore } from "@/features/stores";
import { Pagination } from "@/features/shared";
import { PageContent, PageHeader } from "@/components/layout";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  IconButton,
  Input,
  InlineError,
  Select,
  SkeletonTable,
  StatusBadge,
  Tooltip,
  toast,
} from "@/components/ui";

const PAGE_SIZE_OPTIONS = [20, 50, 100];

const STATUS_OPTIONS = [
  { value: "all", label: "全部状态" },
  { value: "processing", label: "处理中" },
  { value: "success", label: "发布成功" },
  { value: "fail", label: "发布失败" },
  { value: "cancel", label: "已取消" },
];

export function PublishPage({ embedded = false }: { embedded?: boolean } = {}) {
  const navigate = useNavigate();
  const store = usePublishLogsStore();
  const refreshLogs = useRefreshPublishLogs();

  const logsQuery = usePublishLogs({
    page: store.page,
    pageSize: store.pageSize,
    status: store.status,
  });

  const [justRefreshed, setJustRefreshed] = useState(false);
  // 输入即过滤（本地）：记录是分页拉回来的，搜索不该每敲一个字就打一次接口
  const [keyword, setKeyword] = useState("");

  const data = logsQuery.data;
  const all = data?.items ?? [];
  const total = data?.total ?? 0;
  // 本地过滤，不发请求：记录已经分页拉回来了，边打边过滤才是手感对的
  const needle = keyword.trim().toLowerCase();
  const items = needle
    ? all.filter((log) =>
        `${log.detailId ?? ""} ${log.title ?? ""} ${log.shopName ?? ""}`
          .toLowerCase()
          .includes(needle))
    : all;

  const doRefresh = (): void => {
    refreshLogs.mutate(undefined, {
      onSuccess: (result) => {
        setJustRefreshed(true);
        if (result.degraded) {
          toast.warning("平台暂时不可达，已保留本地状态", {
            description: "稍后再点「刷新回执」即可",
          });
          return;
        }
        toast.success(
          result.updated > 0 ? `已更新 ${result.updated} 条回执` : "没有新的回执",
          { description: "回源平台查询 status=processing 的记录" }
        );
        void logsQuery.refetch();
      },
      onError: (error) => {
        toast.error("刷新回执失败", { description: apiErrorMessage(error) });
      },
    });
  };

  return (
    <>
      {!embedded && (
      <PageHeader
        title="发布记录"
        breadcrumb={["奥皇上品", "功能", "发布记录"]}
        description={
          data
            ? `本机共 ${formatCount(total)} 条${data.platformTotal !== null ? ` · 平台侧 ${formatCount(data.platformTotal)} 条` : ""}`
            : undefined
        }
        actions={
          <>
            <Button
              variant="secondary"
              icon={<RefreshCw className={cn(refreshLogs.isPending && "animate-spin")} />}
              loading={refreshLogs.isPending}
              loadingText="回源中…"
              onClick={doRefresh}
            >
              刷新回执
            </Button>
            <Button variant="primary" onClick={() => navigate("/warehouse")} icon={<Undo2 />}>
              回仓库发布
            </Button>
          </>
        }
      />
      )}

      <PageContent width="table" padding={embedded ? "" : "p-6"} scroll={!embedded}>
        <div className="flex flex-wrap items-center gap-2">
          <Select
            size="md"
            aria-label="状态筛选"
            className="w-[148px]"
            value={store.status ?? "all"}
            onChange={(value) => store.setStatus(value === "all" ? null : value)}
            options={STATUS_OPTIONS}
          />
          <Select
            size="md"
            aria-label="每页条数"
            className="w-[124px]"
            value={String(store.pageSize)}
            onChange={(value) => store.setPageSize(Number(value))}
            options={PAGE_SIZE_OPTIONS.map((size) => ({
              value: String(size),
              label: `每页 ${size}`,
            }))}
          />
          {/* 搜索：老版 publish-record-search，按 detailId / 标题 / 店铺过滤。
              发布记录一多就得靠这个找「那件到底成没成」。 */}
          <div className="relative w-[220px]">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-muted" />
            <Input
              size="md"
              className="pl-8"
              placeholder="搜商品 ID / 标题 / 店铺"
              aria-label="搜索发布记录"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
            />
          </div>
          <IconButton
            label="重新加载列表"
            size="md"
            variant="secondary"
            icon={<RefreshCw className={cn(logsQuery.isFetching && "animate-spin")} />}
            onClick={() => void logsQuery.refetch()}
          />
          <div className="ml-auto flex items-center gap-1.5">
            {data?.benchOnly && (
              <Tooltip content="只显示本机发起的发布；平台手工发布的历史不含在内">
                <Badge variant="neutral">仅本机记录</Badge>
              </Tooltip>
            )}
            {data?.degraded && <Badge variant="warning">平台回执未更新</Badge>}
            {justRefreshed && <Badge variant="info">刚刚回源</Badge>}
          </div>
        </div>

        {logsQuery.isLoading && <SkeletonTable rows={8} columns={["w-10", "flex-1", "w-[120px]", "w-[88px]", "w-[72px]", "w-[104px]", "w-[110px]"]} />}

        {logsQuery.isError && (
          <Card>
            <ErrorState
              title="发布记录加载失败"
              description={apiErrorMessage(logsQuery.error)}
              errorCode={logsQuery.error instanceof ApiError ? logsQuery.error.code : null}
              onRetry={() => void logsQuery.refetch()}
              retrying={logsQuery.isFetching}
            />
          </Card>
        )}

        {!logsQuery.isLoading && !logsQuery.isError && items.length === 0 && (
          <Card>
            <EmptyState
              title={needle ? "没有匹配的记录" : store.status ? "当前筛选下没有记录" : "还没有发布记录"}
              description={
                needle
                  ? `本页没有匹配「${keyword.trim()}」的记录，换关键词或翻页再看看。`
                  : store.status
                    ? "试着切换状态筛选，或清除筛选后查看全部记录。"
                    : "在成品仓库选中商品后发布，这里会记录每一次提交与平台回执。"
              }
              action={{
                label: needle ? "清除搜索" : store.status ? "清除筛选" : "去成品仓库",
                onClick: () => {
                  if (needle) setKeyword("");
                  if (store.status) store.setStatus(null);
                  else navigate("/warehouse");
                },
              }}
            />
          </Card>
        )}

        {!logsQuery.isLoading && !logsQuery.isError && items.length > 0 && (
          <div className="overflow-hidden rounded-lg border border-border bg-surface">
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="h-10 bg-canvas-inset/85 backdrop-blur-[2px]">
                    <th className="sticky top-0 border-b border-border px-3 pl-4 text-left text-xs font-medium text-fg-muted">
                      商品
                    </th>
                    <th className="sticky top-0 w-[160px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      店铺
                    </th>
                    <th className="sticky top-0 w-[112px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      状态
                    </th>
                    <th className="sticky top-0 w-[96px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      操作人
                    </th>
                    <th className="sticky top-0 w-[132px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      提交时间
                    </th>
                    <th className="sticky top-0 w-[132px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      完成时间
                    </th>
                    <th className="sticky top-0 border-b border-border px-3 pr-4 text-left text-xs font-medium text-fg-muted">
                      消息 / 平台 id
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((log) => {
                    return (
                      <tr
                        key={log.logId}
                        className="border-b border-border-subtle transition-colors duration-fast last:border-0 hover:bg-surface-hover"
                      >
                        <td className="px-3 py-2.5 pl-4">
                          <div className="min-w-0">
                            <p className="truncate text-[13px] font-medium text-fg">
                              {log.title || `明细 ${log.detailId ?? log.logId}`}
                            </p>
                            <p className="truncate font-mono text-2xs text-fg-muted">
                              {log.detailId ?? "—"}
                              {log.batchId ? ` · ${log.batchId}` : ""}
                              {log.mode ? ` · ${log.mode}` : ""}
                            </p>
                          </div>
                        </td>
                        <td className="px-3 py-2.5">
                          <span className="block truncate text-[13px] text-fg">
                            {log.shopName || log.shopId}
                          </span>
                        </td>
                        <td className="px-3 py-2.5">
                          <StatusBadge meta={publishStatus(log.status)} />
                        </td>
                        <td className="px-3 py-2.5">
                          {/* 操作人：老记录没这个字段，显示「—」而不是猜一个人 */}
                          <span className="truncate text-[13px] text-fg-secondary">
                            {log.operatorName || "—"}
                          </span>
                        </td>
                        <td className="px-3 py-2.5">
                          <span className="num text-xs text-fg-secondary">
                            {formatRelative(log.requestedAt)}
                          </span>
                        </td>
                        <td className="px-3 py-2.5">
                          <span className="num text-xs text-fg-muted">
                            {log.finishedAt ? formatRelative(log.finishedAt) : "—"}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 pr-4">
                          <div className="flex items-center gap-2">
                            <p
                              className={cn(
                                "min-w-0 flex-1 truncate text-xs",
                                log.status === "fail" ? "text-danger-600" : "text-fg-muted"
                              )}
                              title={log.reason}
                            >
                              {log.reason || (log.status === "processing" ? "已提交，平台尚未返回结果" : "—")}
                            </p>
                            {log.itemEditUrl && (
                              <a
                                href={log.itemEditUrl}
                                target="_blank"
                                rel="noreferrer"
                                className="inline-flex shrink-0 items-center gap-1 text-xs font-medium text-brand-700 hover:underline"
                              >
                                编辑
                                <ExternalLink className="size-3" strokeWidth={2} />
                              </a>
                            )}
                            {log.platformItemId && (
                              <span className="num shrink-0 font-mono text-2xs text-fg-muted">
                                {log.platformItemId}
                              </span>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <Pagination
          page={store.page}
          pageSize={store.pageSize}
          total={total}
          hasMore={data?.hasMore}
          pageSizeOptions={PAGE_SIZE_OPTIONS.map((size) => ({ value: size, label: String(size) }))}
          onPageChange={store.setPage}
          onPageSizeChange={store.setPageSize}
          disabled={logsQuery.isFetching}
        />

        {logsQuery.isError && (
          <InlineError onRetry={() => void logsQuery.refetch()}>
            {apiErrorMessage(logsQuery.error)}
          </InlineError>
        )}
      </PageContent>
    </>
  );
}

export default PublishPage;
