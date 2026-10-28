// features/batch —— 批次编排面板（POST /batch 系列）
//
// 后端刻意没有常驻线程：`POST /batch/{id}/step` 一次只推进一件，暂停/停止
// 立刻生效，进程重启也不丢进度。代价是「谁来回调 step」——这个面板负责这件事：
//
//   · 手动：每行一个「推进一件（3/10）」，点一次走一件；
//   · 自动：打开「自动」开关后每 2 秒调一次，直到批次不再是可推进状态。
//
// 自动推进的定时器必须跟着组件卸载一起清掉（切到别的 tab / 别的页面时组件
// 卸载，定时器留着就会在后台一直打接口），见下面的 useEffect 清理。

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  ListChecks,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Square,
  Trash2,
} from "lucide-react";
import { cn, EMPTY_PLACEHOLDER, formatCount, formatDate } from "@/lib/utils";
import { ApiError, apiErrorMessage } from "@/lib/api";
import { isConflictBusy } from "@/lib/conflict";
import {
  useBatch,
  useBatchControl,
  useBatchRetryFailed,
  useBatchStep,
  useBatches,
  useDeleteBatch,
} from "@/lib/queries";
import type { Batch, BatchItem } from "@/lib/types";
import type { BadgeVariant } from "@/components/ui";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  IconButton,
  InlineError,
  Progress,
  SkeletonTable,
  Switch,
  toast,
} from "@/components/ui";

/** 自动推进的节拍。2 秒是「看得见在动」和「不把本地服务打满」之间的折中。 */
const AUTO_STEP_INTERVAL_MS = 2000;

/** 状态 → 徽标文案与色（BatchStatus 全六态） */
const BATCH_STATUS_META: Record<string, { label: string; variant: BadgeVariant }> = {
  pending: { label: "待开始", variant: "neutral" },
  running: { label: "进行中", variant: "info" },
  paused: { label: "已暂停", variant: "warning" },
  stopped: { label: "已停止", variant: "neutral" },
  done: { label: "已完成", variant: "success" },
  failed: { label: "失败", variant: "danger" },
};

/** 子项状态（batch_items.status）：pending / running / ok / failed / skipped */
const ITEM_STATUS_META: Record<string, { label: string; variant: BadgeVariant }> = {
  pending: { label: "待处理", variant: "neutral" },
  running: { label: "提交中", variant: "info" },
  ok: { label: "已完成", variant: "success" },
  failed: { label: "失败", variant: "danger" },
  skipped: { label: "已跳过", variant: "neutral" },
};

/** 后端允许删除的状态（batch_service._DELETABLE）：跑着的删了会丢 current_index */
const DELETABLE_STATUSES = new Set(["done", "stopped", "failed"]);

/**
 * 能推进的状态。
 *
 * 任务书只写了 `running`，但后端 `run_once` 对 `pending` 同样是放行的（第一次
 * step 会把批次翻成 running）。只认 running 的话，刚建出来的批次会卡死：
 * pause 只接受 running/pending、resume 只接受 paused，绕一圈才能动第一件。
 * 所以这里把 pending 也算成可推进，按钮文案区分「开始」和「推进」。
 */
const STEPPABLE_STATUSES = new Set(["pending", "running"]);

function statusMeta(status: string): { label: string; variant: BadgeVariant } {
  return BATCH_STATUS_META[status] ?? { label: status || "未知", variant: "neutral" };
}

function itemStatusMeta(status: string): { label: string; variant: BadgeVariant } {
  return ITEM_STATUS_META[status] ?? { label: status || "未知", variant: "neutral" };
}

/** 成功 / 失败件数：从 results 快照里数，列表接口不带 items，不能按子项查 */
function countsOf(batch: Batch): { total: number; ok: number; failed: number; skipped: number; done: number } {
  const rows = Array.isArray(batch.results) ? batch.results : [];
  let ok = 0;
  let failed = 0;
  let skipped = 0;
  for (const row of rows) {
    if (row?.status === "ok") ok += 1;
    else if (row?.status === "failed") failed += 1;
    else if (row?.status === "skipped") skipped += 1;
  }
  const total = batch.detailIds?.length ?? 0;
  // 进度口径是「成功 + 失败」：跳过（停止时统一标 skipped）不算跑过，
  // 否则一个被停掉的批次会显示 10/10，看起来像跑完了。
  return { total, ok, failed, skipped, done: ok + failed };
}

export interface BatchPanelProps {
  /** 列表条数上限（后端夹在 1..200） */
  limit?: number;
  /** 只看某个账号的批次；null = 全部 */
  accountId?: string | null;
  /** 空状态里的「去选商品」——面板自己不切 tab，交给宿主页 */
  onGoToProducts?: () => void;
  className?: string;
}

export function BatchPanel({ limit = 50, accountId = null, onGoToProducts, className }: BatchPanelProps) {
  const listQuery = useBatches({ limit, accountId });
  const step = useBatchStep();
  const control = useBatchControl();
  const retryFailed = useBatchRetryFailed();
  const removeBatch = useDeleteBatch();

  // 一次只展开一行：详情要单独查 /batch/{id}，同时展开多行等于同时打 N 个请求
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const detailQuery = useBatch(expandedId);

  /** 正在自动推进的批次 id（同一时刻只允许一个，否则两批交替打接口没意义） */
  const [autoId, setAutoId] = useState<string | null>(null);

  const [stopTarget, setStopTarget] = useState<Batch | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Batch | null>(null);
  /** RETRY_UNSAFE 的二次确认：结果未知的件必须人工核对后才允许重跑 */
  const [unsafeTarget, setUnsafeTarget] = useState<{ batch: Batch; message: string } | null>(null);

  const items = useMemo(() => listQuery.data?.items ?? [], [listQuery.data?.items]);
  const total = listQuery.data?.total ?? 0;

  /* ------------------------------------------------------------------ *
   * 自动推进
   *
   * `mutate` 在 TanStack Query v5 里是稳定引用，所以这个 effect 只在
   * autoId 变化时重新武装，不会因为每次渲染重开一个定时器。
   *
   * 两个 ref 各管一件事：
   *   · wantRef —— 用户「还想不想自动推进」，跨 StrictMode 的假卸载存活；
   *   · inFlightRef —— 已经有一次 step 在飞，别再叠一次（同一批次并发 step
   *     会各自推进 current_index 指向的那一件）。
   *
   * 首跳也走 setTimeout 而不是同步调用：React 18 StrictMode 开发模式会
   * effect → cleanup → effect 跑两遍，同步调用就会发两个 step。定时器版本
   * 第一遍排的那个在 cleanup 里被清掉，只剩第二遍的那一个。
   *
   * 清理三件事（顺序重要）：
   *   1. wantRef 置空：在飞的那次 step 回来后不再排下一次；
   *   2. 清掉待触发的 setTimeout；
   *   3. 卸载即执行（effect 的 cleanup 天然如此）。
   * 少了第 1 条，卸载瞬间正好有一次 step 在飞，它的 onSuccess 会把定时器
   * 重新排上，于是切页之后还在打接口——这正是必须清的原因。
   * ------------------------------------------------------------------ */
  const { mutate: stepMutate } = step;
  const wantRef = useRef<string | null>(null);
  const inFlightRef = useRef(false);

  useEffect(() => {
    if (!autoId) {
      wantRef.current = null;
      return;
    }
    wantRef.current = autoId;
    let cancelled = false;
    let timer: number | undefined;

    const schedule = (delay: number): void => {
      timer = window.setTimeout(tick, delay);
    };

    const tick = (): void => {
      const id = wantRef.current;
      if (cancelled || !id) return;
      if (inFlightRef.current) {
        schedule(AUTO_STEP_INTERVAL_MS);
        return;
      }
      inFlightRef.current = true;
      stepMutate(id, {
        onSuccess: (result) => {
          inFlightRef.current = false;
          // cancelled 挡住「关掉又马上打开」时的旧回调：旧 effect 已清理，
          // 它的回调再排一个定时器就会出现两条自动推进链交替打接口。
          if (cancelled || wantRef.current !== id) return;
          const next = result.batch.status;
          if (STEPPABLE_STATUSES.has(next)) {
            schedule(AUTO_STEP_INTERVAL_MS);
            return;
          }
          // 跑完 / 暂停 / 停止 / 失败：自动推进自然结束，不用再问用户
          wantRef.current = null;
          setAutoId(null);
          if (next === "done") {
            toast.success(`批次已完成：${result.message}`);
          } else if (next === "failed") {
            toast.warning("自动推进已停止：有商品失败", { description: result.message });
          }
        },
        onError: (error) => {
          inFlightRef.current = false;
          if (cancelled || wantRef.current !== id) return;
          // 409 CONFLICT_BUSY：别的任务正占着这件商品，是「等一下」不是「出错了」。
          // 自动推进不因此停：wantRef 不动，排下一拍再试同一件。
          if (isConflictBusy(error)) {
            toast.warning(apiErrorMessage(error), {
              description: "自动推进没停，稍后自动重试这一件",
            });
            schedule(AUTO_STEP_INTERVAL_MS);
            return;
          }
          wantRef.current = null;
          setAutoId(null);
          toast.error("自动推进已停止", { description: apiErrorMessage(error) });
        },
      });
    };

    schedule(0);
    return () => {
      cancelled = true;
      wantRef.current = null;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [autoId, stepMutate]);

  const runStep = (batch: Batch): void => {
    step.mutate(batch.id, {
      onSuccess: (result) => {
        if (result.batch.status === "done") {
          toast.success(`批次已完成，共 ${formatCount(countsOf(result.batch).total)} 件`);
          return;
        }
        // 单件失败也是正常返回（批次被置 failed），message 里就是原因
        if (!result.advanced) toast.warning(result.message || "这一件没有推进");
      },
      onError: (error) => {
        const code = error instanceof ApiError ? error.code : null;
        if (code === "RETRY_UNSAFE") {
          // 崩在提交中间的件：必须先去妙手核对，不能盲目重跑
          toast.error("这件上次的保存结果未知", { description: apiErrorMessage(error) });
          return;
        }
        // 409 CONFLICT_BUSY：有别的任务正占着，等一会儿再推进（不是失败）
        if (isConflictBusy(error)) {
          toast.warning(apiErrorMessage(error));
          return;
        }
        toast.error("推进失败", { description: apiErrorMessage(error) });
      },
    });
  };

  const runControl = (
    batch: Batch,
    action: "pause" | "resume" | "stop",
    onSettled?: () => void
  ): void => {
    control.mutate(
      { id: batch.id, action },
      {
        onSuccess: (result) => {
          // 暂停 / 停止都要把自动推进收手，否则下一跳会拿到「已暂停」白打一次接口
          if (action !== "resume") {
            setAutoId((current) => (current === batch.id ? null : current));
          }
          toast.success(
            result.message || (action === "pause" ? "批次已暂停" : action === "stop" ? "批次已停止" : "批次已继续")
          );
        },
        onError: (error) => toast.error("操作失败", { description: apiErrorMessage(error) }),
        onSettled: () => onSettled?.(),
      }
    );
  };

  const runRetry = (batch: Batch, allowUnknown = false): void => {
    retryFailed.mutate(
      { id: batch.id, allowUnknown },
      {
        onSuccess: (result) => {
          setUnsafeTarget(null);
          toast.success(result.message || `已重试 ${result.retried} 件`);
        },
        onError: (error) => {
          const code = error instanceof ApiError ? error.code : null;
          if (code === "RETRY_UNSAFE") {
            setUnsafeTarget({ batch, message: apiErrorMessage(error) });
            return;
          }
          toast.error("重试失败", { description: apiErrorMessage(error) });
        },
      }
    );
  };

  const confirmDelete = (): void => {
    const target = deleteTarget;
    if (!target) return;
    removeBatch.mutate(target.id, {
      onSuccess: (result) => {
        if (expandedId === target.id) setExpandedId(null);
        if (autoId === target.id) setAutoId(null);
        toast.success(`已删除批次（含 ${formatCount(result.items)} 件记录）`);
        setDeleteTarget(null);
      },
      onError: (error) => {
        toast.error("删除失败", { description: apiErrorMessage(error) });
        setDeleteTarget(null);
      },
    });
  };

  /**
   * 控制类按钮的禁用条件。
   *
   * 刻意**不含** `step.isPending`：自动推进时每 2 秒就有一件在飞，把暂停/停止
   * 一起禁掉的话，操作者想中途刹车会一直点不动——而这正是最需要能按下去的时刻。
   * 后端 pause/stop 是纯状态改写，跟正在跑的那一件不冲突。
   */
  const busy = control.isPending || retryFailed.isPending;

  return (
    <div className={cn("flex flex-col gap-4", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-medium text-fg">批次编排</span>
        <span className="num text-xs text-fg-muted">共 {formatCount(total)} 个</span>
        {autoId && (
          <Badge variant="info" icon={<RefreshCw className="size-3 animate-spin" />}>
            自动推进中
          </Badge>
        )}
        <div className="ml-auto flex items-center gap-2">
          <IconButton
            label="刷新批次列表"
            size="sm"
            variant="secondary"
            icon={<RefreshCw className={cn(listQuery.isFetching && "animate-spin")} />}
            onClick={() => void listQuery.refetch()}
          />
        </div>
      </div>

      {listQuery.isLoading && (
        <SkeletonTable
          rows={4}
          columns={[
            "w-[28px]",
            "w-[88px]",
            "w-[60px]",
            "w-[60px]",
            "w-[60px]",
            "w-[132px]",
            "flex-1",
            "w-[220px]",
          ]}
        />
      )}

      {listQuery.isError && (
        <Card>
          <ErrorState
            title="批次列表加载失败"
            description={apiErrorMessage(listQuery.error)}
            errorCode={listQuery.error instanceof ApiError ? listQuery.error.code : null}
            onRetry={() => void listQuery.refetch()}
            retrying={listQuery.isFetching}
          />
        </Card>
      )}

      {!listQuery.isLoading && !listQuery.isError && items.length === 0 && (
        <Card>
          <EmptyState
            icon={<ListChecks strokeWidth={1.5} />}
            title="还没有批次"
            description="在成品仓库里选中商品后创建批次，系统会按顺序一件一件处理，可以随时暂停或停止。"
            action={
              onGoToProducts
                ? { label: "去选商品", onClick: onGoToProducts, icon: <ListChecks /> }
                : undefined
            }
            minHeight={220}
          />
        </Card>
      )}

      {!listQuery.isLoading && !listQuery.isError && items.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="h-10 bg-canvas-inset/85">
                  <th className="w-[28px] border-b border-border pl-3" aria-label="展开" />
                  <th className="w-[88px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    状态
                  </th>
                  <th className="w-[60px] border-b border-border px-3 text-right text-xs font-medium text-fg-muted">
                    总数
                  </th>
                  <th className="w-[60px] border-b border-border px-3 text-right text-xs font-medium text-fg-muted">
                    成功
                  </th>
                  <th className="w-[60px] border-b border-border px-3 text-right text-xs font-medium text-fg-muted">
                    失败
                  </th>
                  <th className="w-[96px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    创建人
                  </th>
                  <th className="w-[132px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    创建时间
                  </th>
                  <th className="w-[168px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    进度
                  </th>
                  <th className="border-b border-border px-3 pr-4 text-right text-xs font-medium text-fg-muted">
                    操作
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((batch) => {
                  const meta = statusMeta(batch.status);
                  const counts = countsOf(batch);
                  const expanded = expandedId === batch.id;
                  const canStep = STEPPABLE_STATUSES.has(batch.status);
                  const canPause = batch.status === "running" || batch.status === "pending";
                  const canResume = batch.status === "paused";
                  const canStop = batch.status === "running" || batch.status === "paused";
                  const canRetry = batch.status === "failed";
                  const canDelete = DELETABLE_STATUSES.has(batch.status);
                  const percent =
                    counts.total > 0 ? Math.round((counts.done / counts.total) * 100) : 0;

                  return (
                    <Fragment key={batch.id}>
                      <tr
                        key={batch.id}
                        className={cn(
                          "cursor-pointer border-b border-border-subtle transition-colors duration-fast",
                          "last:border-0 hover:bg-surface-hover",
                          expanded && "bg-surface-hover"
                        )}
                        onClick={() => setExpandedId(expanded ? null : batch.id)}
                      >
                        <td className="py-2.5 pl-3 align-top">
                          <span className="mt-0.5 grid size-4 place-items-center text-fg-muted">
                            {expanded ? (
                              <ChevronDown className="size-4" />
                            ) : (
                              <ChevronRight className="size-4" />
                            )}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 align-top">
                          <Badge variant={meta.variant}>{meta.label}</Badge>
                          <p className="mt-1 truncate font-mono text-2xs text-fg-muted">{batch.id}</p>
                        </td>
                        <td className="px-3 py-2.5 text-right align-top">
                          <span className="num text-[13px] text-fg">{formatCount(counts.total)}</span>
                        </td>
                        <td className="px-3 py-2.5 text-right align-top">
                          <span className="num text-[13px] text-success-700">
                            {formatCount(counts.ok)}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 text-right align-top">
                          <span
                            className={cn(
                              "num text-[13px]",
                              counts.failed > 0 ? "text-danger-700" : "text-fg-muted"
                            )}
                          >
                            {formatCount(counts.failed)}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 align-top">
                          {/* 创建人：后端没记录时显示「—」，不猜是谁建的 */}
                          <span className="truncate text-[13px] text-fg-secondary">
                            {batch.operatorName || EMPTY_PLACEHOLDER}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 align-top">
                          <p className="num text-[13px] text-fg-secondary">
                            {formatDate(batch.createdAt, "MM-DD HH:mm")}
                          </p>
                          {batch.finishedAt && (
                            <p className="num text-2xs text-fg-muted">
                              结束 {formatDate(batch.finishedAt, "MM-DD HH:mm")}
                            </p>
                          )}
                        </td>
                        <td className="px-3 py-2.5 align-top">
                          <div className="flex items-center gap-2">
                            <span className="num shrink-0 text-[13px] text-fg">
                              {counts.done}/{counts.total}
                            </span>
                            <Progress
                              value={percent}
                              tone={
                                batch.status === "failed"
                                  ? "danger"
                                  : batch.status === "done"
                                    ? "success"
                                    : batch.status === "paused"
                                      ? "warning"
                                      : "brand"
                              }
                              className="min-w-0 flex-1"
                            />
                          </div>
                          <p className="mt-1 text-2xs text-fg-muted">
                            还剩 {formatCount(batch.remaining)} 件
                            {counts.skipped > 0 && `（跳过 ${formatCount(counts.skipped)}）`}
                          </p>
                        </td>
                        <td
                          className="px-3 py-2.5 pr-4 align-top"
                          onClick={(event) => event.stopPropagation()}
                        >
                          <div className="flex flex-wrap items-center justify-end gap-1">
                            {canStep && (
                              <Button
                                size="sm"
                                variant="secondary"
                                // 自动推进时禁掉手动推进：两路同时 step 同一批次，
                                // 后端会各自推进 current_index 指向的那一件
                                disabled={step.isPending || autoId === batch.id}
                                onClick={() => runStep(batch)}
                              >
                                {batch.status === "pending" ? "开始推进" : "推进一件"}（
                                {counts.done}/{counts.total}）
                              </Button>
                            )}
                            {canStep && (
                              <span className="flex items-center gap-1.5 pl-1">
                                <Switch
                                  checked={autoId === batch.id}
                                  onChange={(checked) => {
                                    setAutoId(checked ? batch.id : null);
                                  }}
                                  aria-label="自动推进"
                                  title="每 2 秒自动推进一件，直到跑完或失败"
                                />
                                <span className="text-2xs text-fg-muted">自动</span>
                              </span>
                            )}
                            {canPause && (
                              <Button
                                size="sm"
                                variant="ghost"
                                icon={<Pause />}
                                disabled={busy}
                                onClick={() => runControl(batch, "pause")}
                              >
                                暂停
                              </Button>
                            )}
                            {canResume && (
                              <Button
                                size="sm"
                                variant="ghost"
                                icon={<Play />}
                                disabled={busy}
                                onClick={() => runControl(batch, "resume")}
                              >
                                继续
                              </Button>
                            )}
                            {canStop && (
                              <Button
                                size="sm"
                                variant="ghost"
                                icon={<Square />}
                                disabled={busy}
                                onClick={() => setStopTarget(batch)}
                              >
                                停止
                              </Button>
                            )}
                            {canRetry && (
                              <Button
                                size="sm"
                                variant="secondary"
                                icon={<RotateCcw />}
                                disabled={busy}
                                onClick={() => runRetry(batch)}
                              >
                                重试失败件
                              </Button>
                            )}
                            <IconButton
                              label={`删除批次 ${batch.id}`}
                              size="sm"
                              variant="danger"
                              icon={<Trash2 />}
                              disabled={!canDelete || removeBatch.isPending}
                              title={
                                canDelete
                                  ? "删除批次"
                                  : "只有已完成 / 已停止 / 失败的批次可以删除"
                              }
                              onClick={() => setDeleteTarget(batch)}
                            />
                          </div>
                        </td>
                      </tr>

                      {expanded && (
                        <tr key={`${batch.id}-detail`} className="border-b border-border-subtle">
                          <td colSpan={8} className="bg-canvas-inset/50 px-4 py-3">
                            <BatchItemsDetail
                              loading={detailQuery.isLoading}
                              error={detailQuery.isError ? detailQuery.error : null}
                              items={detailQuery.data?.items ?? []}
                              onRetry={() => void detailQuery.refetch()}
                              shopId={batch.shopId}
                              settings={batch.settings}
                            />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 停止确认：未跑的件会被后端统一标成 skipped，这里必须提前说清 */}
      <ConfirmDialog
        open={stopTarget !== null}
        onOpenChange={(open) => {
          if (!open) setStopTarget(null);
        }}
        title="停止这个批次？"
        description="停止后当前批次立即结束，未跑的件会被标为跳过。"
        confirmLabel="停止批次"
        loading={control.isPending}
        onConfirm={() => {
          if (!stopTarget) return;
          // 先收掉自动推进再发请求：否则这一跳的 step 可能和 stop 抢同一行
          setAutoId((current) => (current === stopTarget.id ? null : current));
          runControl(stopTarget, "stop", () => setStopTarget(null));
        }}
      >
        {stopTarget && (
          <p className="text-[13px] leading-5 text-fg-secondary">
            批次 <span className="font-mono text-2xs">{stopTarget.id}</span> 共{" "}
            <span className="num font-medium">{formatCount(countsOf(stopTarget).total)}</span> 件，
            还剩 <span className="num font-medium">{formatCount(stopTarget.remaining)}</span> 件未处理。
            停止之后这些件不会被「重试失败件」捞回来。
          </p>
        )}
      </ConfirmDialog>

      {/* 删除确认（danger） */}
      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        title="删除这个批次？"
        description="批次的逐件状态会一起删除，此操作不可恢复；已入仓的商品不受影响。"
        confirmLabel="删除批次"
        danger
        loading={removeBatch.isPending}
        onConfirm={confirmDelete}
      >
        {deleteTarget && (
          <p className="text-[13px] leading-5 text-fg-secondary">
            批次 <span className="font-mono text-2xs">{deleteTarget.id}</span> 的{" "}
            <span className="num font-medium">{formatCount(countsOf(deleteTarget).total)}</span> 条
            逐件记录会一起删除。
          </p>
        )}
      </ConfirmDialog>

      {/* RETRY_UNSAFE 二次确认：重跑可能第二次入仓（重量会被再加一次增量） */}
      <ConfirmDialog
        open={unsafeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setUnsafeTarget(null);
        }}
        title="确认已在妙手核对过吗？"
        description="这些件上次的保存结果未知。请先去妙手确认没有保存过，再点确认重试。"
        confirmLabel="已核对，强制重试"
        danger
        loading={retryFailed.isPending}
        onConfirm={() => {
          if (!unsafeTarget) return;
          runRetry(unsafeTarget.batch, true);
        }}
      >
        {unsafeTarget && (
          <div className="flex flex-col gap-2">
            <InlineError>{unsafeTarget.message}</InlineError>
            <p className="text-[13px] leading-5 text-fg-secondary">
              重复入仓会让商品重量被反复加增量（后端 prepare_weight 不幂等），
              所以默认拒绝重跑。确认没保存过再继续。
            </p>
          </div>
        )}
      </ConfirmDialog>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 展开后的逐件列表
 * ------------------------------------------------------------------ */

interface BatchItemsDetailProps {
  loading: boolean;
  error: unknown;
  items: BatchItem[];
  onRetry: () => void;
  shopId: string;
  settings: Batch["settings"];
}

function BatchItemsDetail({ loading, error, items, onRetry, shopId, settings }: BatchItemsDetailProps) {
  const summary = [
    shopId ? `店铺 ${shopId}` : null,
    settings?.template ? `模板 ${settings.template}` : null,
    settings?.language ? `语言 ${settings.language}` : null,
    settings?.processImages ? "处理图片" : null,
  ].filter(Boolean) as string[];

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-2xs text-fg-muted">
        <span>逐件明细</span>
        {summary.map((text) => (
          <Badge key={text} variant="neutral">
            {text}
          </Badge>
        ))}
      </div>

      {loading && <p className="text-xs text-fg-muted">正在加载逐件状态…</p>}

      {!loading && Boolean(error) && (
        <InlineError onRetry={onRetry}>{apiErrorMessage(error)}</InlineError>
      )}

      {!loading && !error && items.length === 0 && (
        <p className="text-xs text-fg-muted">这个批次没有逐件记录。</p>
      )}

      {!loading && !error && items.length > 0 && (
        <div className="max-h-[280px] overflow-auto rounded-md border border-border-subtle bg-surface">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="h-8 bg-canvas-inset/70">
                <th className="w-[48px] border-b border-border-subtle pl-3 text-left font-medium text-fg-muted">
                  #
                </th>
                <th className="w-[140px] border-b border-border-subtle px-3 text-left font-medium text-fg-muted">
                  detailId
                </th>
                <th className="w-[88px] border-b border-border-subtle px-3 text-left font-medium text-fg-muted">
                  状态
                </th>
                <th className="w-[56px] border-b border-border-subtle px-3 text-right font-medium text-fg-muted">
                  尝试
                </th>
                <th className="border-b border-border-subtle px-3 pr-4 text-left font-medium text-fg-muted">
                  错误信息
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => {
                const meta = itemStatusMeta(item.status);
                const unknown = item.message.includes("保存可能已提交");
                return (
                  <tr key={item.id} className="border-b border-border-subtle last:border-0">
                    <td className="py-1.5 pl-3">
                      <span className="num text-fg-muted">{index + 1}</span>
                    </td>
                    <td className="px-3 py-1.5">
                      <span className="font-mono text-fg-secondary">{item.detailId}</span>
                    </td>
                    <td className="px-3 py-1.5">
                      <Badge variant={meta.variant}>{meta.label}</Badge>
                    </td>
                    <td className="px-3 py-1.5 text-right">
                      <span className="num text-fg-muted">{item.attempt}</span>
                    </td>
                    <td className="px-3 py-1.5 pr-4">
                      {item.message ? (
                        <span
                          className={cn(
                            "flex items-start gap-1 leading-5",
                            unknown ? "text-warning-700" : "text-danger-700"
                          )}
                        >
                          {unknown && <AlertTriangle className="mt-0.5 size-3 shrink-0" />}
                          <span className="min-w-0">{item.message}</span>
                        </span>
                      ) : (
                        <span className="text-fg-muted">—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default BatchPanel;
