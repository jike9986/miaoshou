// features/settings —— 审计日志（谁在什么时候动了什么）
//
// 这张表的唯一目的：出了事能查清楚是哪个人做的。
// 因此每条记录都必须能回答「谁 / 做了什么 / 对哪个对象 / 成没成 / 为什么没成」。
//
// 分页方式与仓库其他表格不同：后端用游标（before=时间戳）而不是 page/pageSize，
// 因为日志在不停追加，用 offset 翻页会漏记录也会重复。
// 这里把游标藏在页码后面：翻到第几页就把那一页的 before 记下来，
// 用户看到的仍然是「上一页 / 第 X 页 / 下一页」。

import { useMemo, useState } from "react";
import { RefreshCw, ScrollText, SearchX } from "lucide-react";
import { apiErrorMessage } from "@/lib/api";
import type { AuditLog } from "@/lib/types";
import { useAuditLogs, useOperators } from "@/lib/queries";
import { cn, formatDate, formatRelative } from "@/lib/utils";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Field,
  IconButton,
  SearchInput,
  Select,
  Separator,
  SkeletonTable,
  Tooltip,
} from "@/components/ui";
import { Pagination } from "@/features/shared";
import {
  AUDIT_ACTION_OPTIONS,
  auditActionText,
  auditResultMeta,
} from "@/features/settings/auditMeta";

/* ------------------------------------------------------------------ *
 * 对象列：优先用后端给的 targetLabel（一句话人话），退回 kind + id
 * ------------------------------------------------------------------ */

function targetText(log: AuditLog): { main: string; sub: string | null } {
  const label = log.targetLabel?.trim();
  const id = log.targetId?.trim();
  const kind = log.targetKind?.trim();
  if (label) return { main: label, sub: kind ?? null };
  if (id) return { main: id, sub: kind ?? null };
  if (kind) return { main: kind, sub: null };
  return { main: "—", sub: null };
}

/* ------------------------------------------------------------------ *
 * 主面板
 * ------------------------------------------------------------------ */

export interface AuditLogPanelProps {
  className?: string;
}

const PAGE_SIZE_OPTIONS = [
  { value: 20, label: "20 条/页" },
  { value: 50, label: "50 条/页" },
  { value: 100, label: "100 条/页" },
];

export function AuditLogPanel({ className }: AuditLogPanelProps) {
  const [operatorId, setOperatorId] = useState("");
  const [action, setAction] = useState("");
  const [result, setResult] = useState("");
  const [keyword, setKeyword] = useState("");
  const [limit, setLimit] = useState(50);
  // 每页的游标：cursors[0] 恒为 null（第一页不带 before）
  const [cursors, setCursors] = useState<Array<number | null>>([null]);
  const [page, setPage] = useState(1);

  const operatorsQuery = useOperators();
  const operators = useMemo(
    () => operatorsQuery.data?.operators ?? [],
    [operatorsQuery.data]
  );

  const before = cursors[page - 1] ?? null;
  const logsQuery = useAuditLogs({
    operatorId: operatorId || null,
    action: action || null,
    result: result || null,
    limit,
    before,
  });

  const rows = useMemo(() => logsQuery.data?.items ?? [], [logsQuery.data]);
  const total = logsQuery.data?.total ?? 0;
  const nextBefore = logsQuery.data?.nextBefore ?? null;

  /** 前端侧关键字过滤：后端没有 q 参数，只在当前页内筛，够用且不额外加接口 */
  const visibleRows = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((log) => {
      const haystack = [
        log.operatorName,
        log.action,
        auditActionText(log.action),
        log.targetKind ?? "",
        log.targetLabel ?? "",
        log.targetId ?? "",
        log.message ?? "",
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [rows, keyword]);

  /** 筛选条件变了要回到第一页，否则会拿旧游标查新条件（结果必然为空） */
  const resetPaging = (): void => {
    setCursors([null]);
    setPage(1);
  };

  const changeFilter = (apply: () => void): void => {
    apply();
    resetPaging();
  };

  const goNext = (): void => {
    if (nextBefore === null) return;
    setCursors((previous) => {
      const next = previous.slice(0, page);
      next[page] = nextBefore;
      return next;
    });
    setPage(page + 1);
  };

  const goPrev = (): void => {
    if (page <= 1) return;
    setPage(page - 1);
  };

  const hasMore = nextBefore !== null;

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {/* 筛选区 */}
      <Card padding="compact">
        <div className="flex flex-wrap items-end gap-2.5">
          <div className="w-[168px]">
            <Field label="操作员">
              <Select
                size="sm"
                aria-label="按操作员筛选"
                value={operatorId}
                onChange={(value) => changeFilter(() => setOperatorId(value))}
                options={[
                  { value: "", label: "全部操作员" },
                  ...operators.map((item) => ({
                    value: item.id,
                    label: item.enabled ? item.name : `${item.name}（已停用）`,
                  })),
                ]}
              />
            </Field>
          </div>
          <div className="w-[176px]">
            <Field label="动作">
              <Select
                size="sm"
                aria-label="按动作筛选"
                value={action}
                onChange={(value) => changeFilter(() => setAction(value))}
                options={AUDIT_ACTION_OPTIONS}
              />
            </Field>
          </div>
          <div className="w-[132px]">
            <Field label="结果">
              <Select
                size="sm"
                aria-label="按结果筛选"
                value={result}
                onChange={(value) => changeFilter(() => setResult(value))}
                options={[
                  { value: "", label: "全部结果" },
                  { value: "success", label: "成功" },
                  { value: "failed", label: "失败" },
                  { value: "blocked", label: "已拦截" },
                ]}
              />
            </Field>
          </div>
          <div className="min-w-[200px] flex-1">
            <Field label="搜索" hint="只在当前页内查找">
              <SearchInput
                value={keyword}
                placeholder="操作员 / 动作 / 对象 / 说明"
                debounceMs={0}
                onChange={(next) => setKeyword(next)}
              />
            </Field>
          </div>
          <div className="flex items-center gap-2 pb-0.5">
            <Tooltip content="重新拉取当前页">
              <IconButton
                label="刷新"
                size="md"
                variant="secondary"
                icon={<RefreshCw className={cn(logsQuery.isFetching && "animate-spin")} />}
                onClick={() => void logsQuery.refetch()}
              />
            </Tooltip>
            <Button
              variant="ghost"
              size="md"
              disabled={
                !operatorId && !action && !result && !keyword && page === 1 && limit === 50
              }
              onClick={() => {
                setOperatorId("");
                setAction("");
                setResult("");
                setKeyword("");
                setLimit(50);
                resetPaging();
              }}
            >
              清空筛选
            </Button>
          </div>
        </div>
      </Card>

      {/* 表格 */}
      <Card padding="none">
        {logsQuery.isLoading ? (
          <SkeletonTable
            rows={8}
            columns={["w-[124px]", "w-[88px]", "w-[132px]", "flex-1", "w-[72px]", "flex-1"]}
            className="rounded-none border-0"
          />
        ) : logsQuery.isError ? (
          <ErrorState
            title="审计日志加载失败"
            description={apiErrorMessage(logsQuery.error)}
            errorCode={
              typeof logsQuery.error === "object" && logsQuery.error !== null && "code" in logsQuery.error
                ? String((logsQuery.error as { code?: string }).code ?? "")
                : null
            }
            onRetry={() => void logsQuery.refetch()}
            retrying={logsQuery.isFetching}
            minHeight={260}
          />
        ) : visibleRows.length === 0 ? (
          <EmptyState
            icon={keyword ? <SearchX strokeWidth={1.5} /> : <ScrollText strokeWidth={1.5} />}
            title={keyword ? "当前页没有匹配的记录" : "还没有操作记录"}
            description={
              keyword
                ? "换个关键字，或清空搜索看这一页的全部记录。"
                : "等有人开始改商品、发批次、动操作员，这里就会按时间倒序记下来。"
            }
            action={keyword ? { label: "清空搜索", onClick: () => setKeyword("") } : undefined}
            minHeight={260}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="h-9 bg-canvas-inset/60">
                  <th className="w-[132px] border-b border-border px-4 text-left text-xs font-medium text-fg-muted">
                    时间
                  </th>
                  <th className="w-[104px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    操作员
                  </th>
                  <th className="w-[136px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    动作
                  </th>
                  <th className="border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    对象
                  </th>
                  <th className="w-[84px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    结果
                  </th>
                  <th className="border-b border-border px-3 pr-4 text-left text-xs font-medium text-fg-muted">
                    说明
                  </th>
                </tr>
              </thead>
              <tbody>
                {visibleRows.map((log) => {
                  const meta = auditResultMeta(log.result);
                  const target = targetText(log);
                  return (
                    <tr
                      key={log.id}
                      className={cn(
                        "border-b border-border-subtle transition-colors duration-fast last:border-0",
                        log.result === "failed" && "bg-danger-50/30",
                        log.result === "blocked" && "bg-warning-50/30",
                        "hover:bg-surface-hover"
                      )}
                    >
                      <td className="px-4 py-2.5">
                        <span className="num text-xs text-fg-secondary">
                          {formatDate(log.createdAt, "MM-DD HH:mm:ss")}
                        </span>
                        <p className="mt-0.5 text-2xs text-fg-muted">
                          {formatRelative(log.createdAt)}
                        </p>
                      </td>
                      <td className="px-3 py-2.5">
                        <span className="text-[13px] text-fg">{log.operatorName || "—"}</span>
                      </td>
                      <td className="px-3 py-2.5">
                        <span className="text-[13px] text-fg">{auditActionText(log.action)}</span>
                        <p className="mt-0.5 truncate font-mono text-2xs text-fg-muted">
                          {log.action}
                        </p>
                      </td>
                      <td className="px-3 py-2.5">
                        <p className="line-clamp-2 break-words text-[13px] leading-5 text-fg">
                          {target.main}
                        </p>
                        {target.sub && (
                          <p className="mt-0.5 text-2xs text-fg-muted">{target.sub}</p>
                        )}
                      </td>
                      <td className="px-3 py-2.5">
                        <Badge variant={meta.variant}>{meta.label}</Badge>
                      </td>
                      <td className="px-3 py-2.5 pr-4">
                        {log.message ? (
                          <p className="line-clamp-2 break-words text-xs leading-[18px] text-fg-secondary">
                            {log.message}
                          </p>
                        ) : (
                          <span className="text-xs text-fg-muted">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <Separator />

        <div className="px-4 py-2.5">
          <Pagination
            page={page}
            pageSize={limit}
            total={total}
            hasMore={hasMore}
            pageSizeOptions={PAGE_SIZE_OPTIONS}
            onPageChange={(next) => {
              if (next > page) goNext();
              else goPrev();
            }}
            onPageSizeChange={(size) => {
              setLimit(size);
              resetPaging();
            }}
            disabled={logsQuery.isFetching}
            showTotal
            showPageNumber
            extra={
              keyword && visibleRows.length !== rows.length ? (
                <span className="text-2xs text-fg-muted">
                  本页筛出 {visibleRows.length} / {rows.length} 条
                </span>
              ) : null
            }
          />
        </div>
      </Card>
    </div>
  );
}

export default AuditLogPanel;
