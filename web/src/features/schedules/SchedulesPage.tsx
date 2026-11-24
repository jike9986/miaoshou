// features/schedules —— 定时发布（api.md §9）
// 列表：启用开关、频率、时间、每次数量、店铺、下次执行、最近结果。
// 新建/编辑：名称、频率（daily/weekly/monthly/range）、时间、星期几、起止日期、每次数量、店铺、模板。
// 删除确认 + 立即执行（演练）。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  CalendarClock,
  Check,
  PackageCheck,
  History,
  Play,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { cn, formatRelative } from "@/lib/utils";
import { ApiError, apiErrorMessage, api } from "@/lib/api";
import {
  LIMITS,
  SCHEDULE_FREQUENCY_OPTIONS
} from "@/lib/constants";
import {
  useAuthStatus,
  useCreateSchedule,
  useDeleteSchedule,
  usePatchSchedule,
  useRunSchedule,
  useSchedules,
  useShops,
  useTemplates,
  useScheduleLogs,
  useWarehouseList,
} from "@/lib/queries";
import type { Schedule, ScheduleFrequency, Shop } from "@/lib/types";
import { EMPTY_SCHEDULE_FORM, type ScheduleFormDraft } from "@/features/stores";
import { ConnectHintBar } from "@/features/shared";
import { useConnectionState } from "@/features/auth/guard";
import { PageContent, PageHeader } from "@/components/layout";
import { Tabs, tabId, tabPanelId } from "@/components/ui";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  IconButton,
  InlineError,
  Input,
  NumberInput,
  Select,
  Separator,
  SkeletonTable,
  StatusBadge,
  Switch,
  toast,
} from "@/components/ui";

const WEEKDAYS = [
  { value: 1, label: "周一" },
  { value: 2, label: "周二" },
  { value: 3, label: "周三" },
  { value: 4, label: "周四" },
  { value: 5, label: "周五" },
  { value: 6, label: "周六" },
  { value: 7, label: "周日" },
];

const FREQUENCY_LABEL: Record<string, string> = {
  daily: "每天",
  weekly: "每周",
  monthly: "每月",
  range: "日期区间",
};

function frequencyText(task: Schedule): string {
  const base = FREQUENCY_LABEL[task.frequency] ?? task.frequency;
  if (task.frequency === "weekly" && task.days.length > 0) {
    const days = task.days
      .map((day) => WEEKDAYS.find((item) => item.value === day)?.label ?? `周${day}`)
      .join("、");
    return `${base}（${days}）`;
  }
  if (task.frequency === "monthly" && task.dayOfMonth.length > 0) {
    return `${base}（${task.dayOfMonth.join("、")} 日）`;
  }
  if (task.frequency === "range" && task.startDate && task.endDate) {
    return `${base}（${task.startDate} → ${task.endDate}）`;
  }
  return base;
}

/** 定时发布 + 发布记录合并为一个板块：上面的标签切换，两边功能都保留。 */
export function SchedulesPage({ tab: initialTab }: { tab?: "schedules" | "history" } = {}) {
  const navigate = useNavigate();
  const { connected, blocked } = useConnectionState();
  const { data: auth } = useAuthStatus();
  const schedulesQuery = useSchedules(auth?.accountId ? { accountId: auth.accountId } : {}, {
    enabled: connected,
  });
  const shopsQuery = useShops(auth?.accountId, { enabled: connected });
  const templatesQuery = useTemplates(connected);

  const createSchedule = useCreateSchedule();
  const patchSchedule = usePatchSchedule();
  const deleteSchedule = useDeleteSchedule();
  const runSchedule = useRunSchedule();

  const [editing, setEditing] = useState<Schedule | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Schedule | null>(null);
  const [runTarget, setRunTarget] = useState<Schedule | null>(null);

  const shops = useMemo(() => shopsQuery.data?.shops ?? [], [shopsQuery.data?.shops]);
  const templates = templatesQuery.data?.templates ?? [];
  const items = schedulesQuery.data?.items ?? [];

  const toggleEnabled = (task: Schedule, enabled: boolean): void => {
    patchSchedule.mutate(
      { id: task.id, patch: { enabled } },
      {
        onSuccess: () => {
          toast.success(enabled ? `已启用「${task.name}」` : `已停用「${task.name}」`);
        },
        onError: (error) => toast.error("更新失败", { description: apiErrorMessage(error) }),
      }
    );
  };

  const submitRun = (): void => {
    const task = runTarget;
    if (!task) return;
    runSchedule.mutate(task.id, {
      onSuccess: (result) => {
        setRunTarget(null);
        const label =
          result.status === "ok"
            ? "执行完成"
            : result.status === "submitted"
              ? "已提交，等待平台回执"
              : result.status === "partial"
              ? "部分完成"
              : result.status === "empty"
                ? "没有可处理的商品"
                : "执行失败";
        const options = {
          description:
            result.status === "empty"
              ? result.reason || "仓库里没有可处理的商品"
              : `计划 ${result.requested} 件 · 成功 ${result.count} 件 · 待回执 ${result.submitted ?? 0} 件 · 失败 ${result.failed} 件${
                  result.reason ? ` · ${result.reason}` : ""
                }`,
        };
        if (result.status === "ok") toast.success(label, options);
        else if (result.status === "submitted") toast.info(label, options);
        else if (result.status === "failed") toast.error(label, options);
        else toast.warning(label, options);
      },
      onError: (error) => {
        toast.error("立即执行失败", { description: apiErrorMessage(error) });
      },
    });
  };

  const confirmDelete = (): void => {
    const task = deleteTarget;
    if (!task) return;
    deleteSchedule.mutate(task.id, {
      onSuccess: (result) => {
        toast.success(`已删除「${task.name}」`, {
          description: `保留 ${result.logsKept} 条历史发布记录`,
        });
        setDeleteTarget(null);
      },
      onError: (error) => toast.error("删除失败", { description: apiErrorMessage(error) }),
    });
  };

  const [tab, setTab] = useState<"schedules" | "history">(initialTab === "history" ? "history" : "schedules");

  if (tab === "history") {
    return (
      <>
        <PageHeader
          title="定时发布"
        />
        <PageContent width="table" padding="p-0" scroll={false}>
          <div className="border-b border-border-subtle px-6 pt-3">
            <Tabs
              value={tab}
              onChange={(value) => setTab(value as "schedules" | "history")}
              bordered={false}
              items={[
                { value: "schedules", label: "定时任务" },
                { value: "history", label: "执行历史" },
              ]}
            />
          </div>
          <div
            role="tabpanel"
            id={tabPanelId("schedules", "history")}
            aria-labelledby={tabId("schedules", "history")}
            className="contents"
          >
            <ScheduleHistoryPanel />
          </div>
        </PageContent>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="定时发布"
        description={schedulesQuery.data ? `共 ${schedulesQuery.data.total} 个任务` : undefined}
        actions={
          <>
            <IconButton
              label="刷新"
              size="md"
              variant="secondary"
              icon={<RefreshCw className={cn(schedulesQuery.isFetching && "animate-spin")} />}
              onClick={() => void schedulesQuery.refetch()}
              disabled={!connected}
            />
            <Button
              variant="primary"
              icon={<Plus />}
              disabled={!connected}
              onClick={() => {
                setEditing(null);
                setFormOpen(true);
              }}
            >
              新建任务
            </Button>
          </>
        }
      />

      <PageContent width="table" padding="p-0">
        <div className="px-6 pt-3">
          <Tabs
            value={tab}
            onChange={(value) => setTab(value as "schedules" | "history")}
            bordered={false}
            idBase="schedules"
            items={[
              { value: "schedules", label: "定时任务" },
              { value: "history", label: "执行历史" },
            ]}
          />
        </div>
        <div
          role="tabpanel"
          id={tabPanelId("schedules", "schedules")}
          aria-labelledby={tabId("schedules", "schedules")}
          className="px-6 pb-6 pt-3"
        >
        {blocked && (
          <ConnectHintBar
            message="定时发布需要连接妙手账号后才能创建与管理。"
            onGoUnlock={() => navigate("/unlock", { state: { from: "/schedules" } })}
          />
        )}

        {!connected && (
          <Card>
            <EmptyState
              icon={<CalendarClock strokeWidth={1.5} />}
              title="连接妙手后才能使用定时发布"
              action={{
                label: "去连接妙手",
                onClick: () => navigate("/unlock", { state: { from: "/schedules" } }),
              }}
            />
          </Card>
        )}

        {connected && schedulesQuery.isLoading && (
          <SkeletonTable rows={5} columns={["w-8", "flex-1", "w-[120px]", "w-[88px]", "w-[132px]", "w-[96px]"]} />
        )}

        {connected && schedulesQuery.isError && (
          <Card>
            <ErrorState
              title="定时任务加载失败"
              description={apiErrorMessage(schedulesQuery.error)}
              errorCode={schedulesQuery.error instanceof ApiError ? schedulesQuery.error.code : null}
              onRetry={() => void schedulesQuery.refetch()}
              retrying={schedulesQuery.isFetching}
            />
          </Card>
        )}

        {connected && !schedulesQuery.isLoading && !schedulesQuery.isError && items.length === 0 && (
          <Card>
            <EmptyState
              icon={<CalendarClock strokeWidth={1.5} />}
              title="还没有定时任务"
              action={{
                label: "新建任务",
                onClick: () => {
                  setEditing(null);
                  setFormOpen(true);
                },
              }}
            />
          </Card>
        )}

        {connected && !schedulesQuery.isLoading && !schedulesQuery.isError && items.length > 0 && (
          <div className="overflow-hidden rounded-lg border border-border bg-surface">
            <div className="w-full">
              <table className="w-full table-fixed border-collapse text-sm">
                <thead>
                  <tr className="h-10 bg-canvas-inset/85 backdrop-blur-[2px]">
                    <th className="sticky top-0 w-[64px] border-b border-border pl-4 text-left text-xs font-medium text-fg-muted">
                      启用
                    </th>
                    <th className="sticky top-0 border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      任务
                    </th>
                    <th className="sticky top-0 w-[18%] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      频率 / 时间
                    </th>
                    <th className="sticky top-0 w-[14%] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      店铺
                    </th>
                    <th className="sticky top-0 w-[18%] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      状态
                    </th>
                    <th className="sticky top-0 w-[18%] border-b border-border px-3 pr-4 text-right text-xs font-medium text-fg-muted">
                      操作
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((task) => (
                    <tr
                      key={task.id}
                      className="border-b border-border-subtle transition-colors duration-fast last:border-0 hover:bg-surface-hover"
                    >
                      <td className="py-2.5 pl-4">
                        <Switch
                          checked={task.enabled}
                          onChange={(checked) => toggleEnabled(task, checked)}
                          aria-label={`${task.enabled ? "停用" : "启用"} ${task.name}`}
                          disabled={patchSchedule.isPending}
                        />
                      </td>
                      <td className="px-3 py-2.5">
                        <p className="truncate text-[13px] font-medium text-fg">{task.name}</p>
                        <p className="truncate font-mono text-2xs text-fg-muted">
                          {task.selectedProductIds?.length
                            ? `已选 ${task.selectedProductIds.length} 件 · 每次 ${task.perRun} 件 · 共 ${task.plannedRuns ?? 0} 轮`
                            : `自动选品 · 每次 ${task.perRun} 件`}
                          {task.claimOnly ? " · 仅认领" : ""}
                        </p>
                      </td>
                      <td className="px-3 py-2.5">
                        <p className="text-[13px] text-fg">{frequencyText(task)}</p>
                        <p className="num text-2xs text-fg-muted">{task.time}</p>
                      </td>
                      <td className="px-3 py-2.5">
                        <p className="truncate text-[13px] text-fg">{task.shopName || task.shopId}</p>
                        {(task.targets?.length ?? 0) > 1 && (
                          <p className="text-2xs text-fg-muted">
                            共 {task.targets?.length} 个店铺
                          </p>
                        )}
                      </td>
                      <td className="px-3 py-2.5">
                        <p className="flex items-center gap-1.5 text-[13px] text-fg">
                          {task.running && (
                            <span
                              className="inline-block size-1.5 animate-pulse rounded-full bg-brand-600"
                              aria-hidden="true"
                            />
                          )}
                          {task.running
                            ? "执行中"
                            : task.completed
                              ? "已完成"
                              : !task.enabled
                                ? "已暂停"
                                : !task.lastRunDate && task.nextDueAt && task.nextDueAt * 1000 <= Date.now()
                                  ? "即将开始"
                                  : !task.lastRunDate
                                    ? "未开始"
                                    : "进行中"}
                        </p>
                        <p className="truncate text-2xs text-fg-muted">
                          {task.running
                            ? "正在发布，请稍候"
                            : task.enabled
                              ? task.nextDueText || "等待执行"
                              : "可重新启用"}
                        </p>
                        {/* 只在有实际结果时显示上次行，避免全零噪音 */}
                        {task.lastResult?.status && (task.lastResult.count > 0 || task.lastResult.failed > 0 || (task.lastResult.submitted ?? 0) > 0) && (
                          <p className="flex items-center gap-1 text-2xs text-fg-muted">
                            <StatusBadge
                              meta={
                                task.lastResult.status === "ok"
                                  ? { label: "上次成功", tone: "success", dot: "success" }
                                  : task.lastResult.status === "submitted"
                                    ? { label: "上次待回执", tone: "neutral", dot: "muted" }
                                  : task.lastResult.status === "partial"
                                    ? { label: "上次部分", tone: "warning", dot: "warning" }
                                    : task.lastResult.status === "empty"
                                      ? { label: "上次无商品", tone: "neutral", dot: "muted" }
                                      : { label: "上次失败", tone: "danger", dot: "danger" }
                              }
                            />
                            <span className="num">
                              {task.lastResult.count > 0 && `成功 ${task.lastResult.count}`}
                              {(task.lastResult.submitted ?? 0) > 0 && ` · 待回执 ${task.lastResult.submitted}`}
                              {task.lastResult.failed > 0 && ` · 失败 ${task.lastResult.failed}`}
                            </span>
                          </p>
                        )}
                      </td>
                      <td className="px-3 py-2.5 pr-4">
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => {
                              setEditing(task);
                              setFormOpen(true);
                            }}
                          >
                            编辑
                          </Button>
                          <IconButton
                            label={`立即执行 ${task.name}`}
                            size="sm"
                            icon={<Play />}
                            disabled={runSchedule.isPending}
                            onClick={() => setRunTarget(task)}
                          />
                          <IconButton
                            label={`删除 ${task.name}`}
                            size="sm"
                            variant="danger"
                            icon={<Trash2 />}
                            onClick={() => setDeleteTarget(task)}
                          />
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
        </div>
      </PageContent>

      <ScheduleFormDrawer
        open={formOpen}
        onOpenChange={(open) => {
          setFormOpen(open);
          if (!open) setEditing(null);
        }}
        task={editing}
        accountId={auth?.accountId ?? ""}
        accounts={auth?.accounts?.map((item) => ({ id: item.id, name: item.name })) ?? []}
        shops={shops}
        templates={templates}
        selectedTemplates={templatesQuery.data?.selected}
        submitting={createSchedule.isPending || patchSchedule.isPending}
        onSubmit={async (payloads) => {
          if (editing) {
            // 编辑时始终是单条任务
            const payload = payloads[0];
            if (!payload) return;
            // 只提交真正改过的字段（api.md §9.3 局部更新）
            const patch: Record<string, unknown> = {};
            if (payload.name !== editing.name) patch.name = payload.name;
            if (payload.enabled !== editing.enabled) patch.enabled = payload.enabled;
            if (payload.accountId !== editing.accountId) patch.accountId = payload.accountId;
            if (payload.shopId !== editing.shopId) patch.shopId = payload.shopId;
            if (payload.template !== editing.template) patch.template = payload.template;
            if (payload.frequency !== editing.frequency) patch.frequency = payload.frequency;
            if (payload.time !== editing.time) patch.time = payload.time;
            if (payload.perRun !== editing.perRun) patch.perRun = payload.perRun;
            if (JSON.stringify(payload.selectedProductIds) !== JSON.stringify(editing.selectedProductIds ?? [])) patch.selectedProductIds = payload.selectedProductIds;
            if (payload.plannedRuns !== editing.plannedRuns) patch.plannedRuns = payload.plannedRuns;
            if (payload.claimOnly !== editing.claimOnly) patch.claimOnly = payload.claimOnly;
            if (JSON.stringify(payload.days) !== JSON.stringify(editing.days)) patch.days = payload.days;
            if (JSON.stringify(payload.dayOfMonth) !== JSON.stringify(editing.dayOfMonth)) {
              patch.dayOfMonth = payload.dayOfMonth;
            }
            if (payload.startDate !== editing.startDate) patch.startDate = payload.startDate;
            if (payload.endDate !== editing.endDate) patch.endDate = payload.endDate;
            // 多店铺：和老任务比一下店铺集合/模板有没有变，变了才提交 targets。
            // 老任务（targets 为空）比较时按「只有一个 shopId」折算，避免一保存就变成多店铺。
            const editingTargets = (editing.targets && editing.targets.length > 0)
              ? editing.targets
              : [{ shopId: editing.shopId, template: editing.template }];
            if (JSON.stringify(payload.targets ?? []) !== JSON.stringify(editingTargets)) {
              patch.targets = payload.targets ?? [];
            }

            if (Object.keys(patch).length === 0) {
              toast.info("没有修改需要保存");
              setFormOpen(false);
              setEditing(null);
              return;
            }
            patchSchedule.mutate(
              { id: editing.id, patch },
              {
                onSuccess: () => {
                  toast.success("已保存定时任务");
                  setFormOpen(false);
                  setEditing(null);
                },
                onError: (error) => toast.error("保存失败", { description: apiErrorMessage(error) }),
              }
            );
            return;
          }
          // 新建：逐账号 fan-out
          if (payloads.length === 1) {
            createSchedule.mutate(payloads[0], {
              onSuccess: () => {
                toast.success("已创建定时任务");
                setFormOpen(false);
              },
              onError: (error) => toast.error("创建失败", { description: apiErrorMessage(error) }),
            });
          } else {
            let created = 0;
            const failMsgs: string[] = [];
            for (const payload of payloads) {
              try {
                await createSchedule.mutateAsync(payload);
                created++;
              } catch (error) {
                failMsgs.push(apiErrorMessage(error));
              }
            }
            if (created > 0) {
              toast.success(`已为 ${created} 个账号创建定时任务`);
              setFormOpen(false);
            }
            if (failMsgs.length > 0) {
              toast.error(`${failMsgs.length} 个账号创建失败`, { description: failMsgs[0] });
            }
          }
        }}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        title={`删除任务「${deleteTarget?.name ?? ""}」？`}
        description="历史发布记录会保留（scheduleId 置空），任务本身不可恢复。"
        confirmLabel="删除任务"
        danger
        loading={deleteSchedule.isPending}
        onConfirm={confirmDelete}
      />

      <ConfirmDialog
        open={runTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRunTarget(null);
        }}
        title={`立即执行「${runTarget?.name ?? ""}」？`}
        description="演练用：同步执行一轮，最长等待 120 秒。会真实发布商品，不可撤销。"
        confirmLabel="立即执行"
        loading={runSchedule.isPending}
        onConfirm={submitRun}
      >
        <div className="flex flex-col gap-2 text-[13px] leading-5 text-fg-secondary">
          <p>
            目标店铺：<span className="font-medium text-fg">{runTarget?.shopName || runTarget?.shopId}</span>
          </p>
          <p>
            本轮计划：<span className="num font-medium text-fg">{runTarget?.perRun ?? 0}</span> 件
            {runTarget?.claimOnly ? "（只认领，不上架）" : ""}
          </p>
          <p>
            定价模板：<span className="font-medium text-fg">{runTarget?.template || "未设置"}</span>
          </p>
        </div>
      </ConfirmDialog>
    </>
  );
}

/* ------------------------------------------------------------------ *
 * 新建 / 编辑抽屉（api.md §9.2）
 * ------------------------------------------------------------------ */

import { ProductPickerDialog } from "@/features/schedules/ProductPickerDialog";
import { DatePicker } from "@/features/schedules/DatePicker";
import { TimePicker } from "@/features/schedules/TimePicker";

/** 当前时间默认值：HH:MM 与 YYYY-MM-DD。 */
function nowTime(): string {
  const now = new Date();
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

function nowDate(): string {
  const now = new Date();
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** 任务名默认值：定时发布-20261105-1930（创建那一刻的年月日-时分）。 */
function defaultScheduleName(): string {
  const now = new Date();
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `定时发布-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
}

/** 今天执行时，把已过的时间点禁用；其他日期不限。 */
function todayMinTime(draft: { frequency: ScheduleFrequency; startDate: string }): string | undefined {
  const now = new Date();
  const pad = (value: number): string => String(value).padStart(2, "0");
  const todayIso = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const isToday = draft.frequency === "once" || draft.frequency === "range"
    ? draft.startDate === todayIso
    : true;
  if (!isToday) return undefined;
  return `${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

interface ScheduleFormDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  task: Schedule | null;
  accountId: string;
  /** 已连接的账号，用来给任务选「用哪个账号跑」（老版 schedule-account） */
  accounts: Array<{ id: string; name: string }>;
  shops: Array<{ shopId: string; name: string; site: string; siteName: string }>;
  templates: Array<{
    name: string;
    invalid?: boolean;
    template?: Record<string, unknown>;
  }>;
  /** 账号 → 当前模板名；新建任务时用它当默认值 */
  selectedTemplates?: Record<string, string>;
  submitting: boolean;
  onSubmit: (payloads: Array<{
    name: string;
    enabled: boolean;
    accountId: string;
    shopId: string;
    template: string;
    /** 一次发多个店铺时带上完整列表；[{shopId, template}] */
    targets?: Array<{ shopId: string; template: string }>;
    frequency: ScheduleFrequency;
    days: number[];
    dayOfMonth: number[];
    startDate: string;
    endDate: string;
    time: string;
    perRun: number;
    selectedProductIds: string[];
    plannedRuns: number | null;
    claimOnly: boolean;
  }>) => void;
}

function ScheduleFormDrawer({
  open,
  onOpenChange,
  task,
  accountId,
  accounts,
  shops,
  templates,
  selectedTemplates,
  submitting,
  onSubmit,
}: ScheduleFormDrawerProps) {
  const [draft, setDraft] = useState<ScheduleFormDraft>(EMPTY_SCHEDULE_FORM);
  const [enabled, setEnabled] = useState(true);
  const [shopId, setShopId] = useState("");
  const [template, setTemplate] = useState("");
  const [selectedProductIds, setSelectedProductIds] = useState<string[]>([]);
  const [plannedRuns, setPlannedRuns] = useState(1);
  const [productPage, setProductPage] = useState(1);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerSelected, setPickerSelected] = useState<string[]>([]);
  /**
   * 用户是否手动选过模板。
   *
   * 用 ref 而不是 state：它只用来阻止「异步数据晚到时覆盖用户输入」，
   * 不需要触发重渲染。用 state 会让每次选择都多一次渲染，而且
   * 把它放进依赖数组会引入新的循环风险。
   */
  const templateTouchedRef = useRef(false);
  // 任务绑定哪个账号执行。老版可以选；新版原先写死当前账号，
  // 一旦切账号，老任务就会用错账号。默认跟随当前账号。
  const [runAccountId, setRunAccountId] = useState(accountId);

  // 老师多账号模式：同时为多个账号创建同一套定时任务。
  // 负责人 2026-11-18：去掉 role 门控，只按「有几个账号」判断。
  // 原来这里写死 identity?.role !== "member"，与发布弹窗（只看 accounts.length > 1）
  // 口径不一致 —— 同一个学员账号在仓库页能选多账号，在定时任务里选不了。
  // 已经第三次出现这种「role 门控漂移」，所以统一成同一个判据。
  const isTeacher = accounts.length > 1;
  // 多账号模式下勾选的账号 id 列表
  const [checkedAccountIds, setCheckedAccountIds] = useState<string[]>([]);
  // 各账号的店铺列表（首次勾选时懒加载）；null = 加载中，[] = 已加载但无店铺，[...] = 有数据
  const [shopsByAccount, setShopsByAccount] = useState<Record<string, Shop[] | null>>({});
  // 各账号勾选的店铺（可多个）；未勾时默认取该账号第一个店铺。
  // 负责人 2026-11-19：定时任务也要能一次发多个店铺，和发布弹窗对齐。
  const [selectedShopIdsByAccount, setSelectedShopIdsByAccount] =
    useState<Record<string, string[]>>({});
  // 站点 → 该站点的模板名（一个站点一套，取第一个）。
  // 多店铺时每个店铺用它自己站点的模板：马来模板套到菲律宾店必算错价。
  const templateNameBySite = useMemo(() => {
    const map: Record<string, string> = {};
    for (const item of templates) {
      if (item.invalid) continue;
      const site = String(
        (item.template as { site?: unknown } | undefined)?.site ?? ""
      ).trim().toUpperCase();
      if (site && !map[site]) map[site] = item.name;
    }
    return map;
  }, [templates]);
  /** 某个店铺该用哪套模板：按它自己的站点找，找不到回落到全局选的模板。 */
  const templateForShop = useCallback(
    (shop: { site?: string } | undefined): string => {
      const site = String(shop?.site ?? "").trim().toUpperCase();
      return (site && templateNameBySite[site]) || template;
    },
    [templateNameBySite, template]
  );
  /** 某个账号本次要发的店铺列表（没勾就回落第一个）。 */
  const targetsForAccount = useCallback(
    (accId: string): Array<{ shopId: string; template: string }> => {
      const accShops = shopsByAccount[accId] ?? [];
      const picked = selectedShopIdsByAccount[accId] ?? [];
      const chosen = picked.length > 0
        ? accShops.filter((shop) => picked.includes(shop.shopId))
        : accShops.slice(0, 1);
      return chosen.map((shop) => ({
        shopId: shop.shopId,
        template: templateForShop(shop),
      }));
    },
    [shopsByAccount, selectedShopIdsByAccount, templateForShop]
  );

  // 切了账号就要换店铺列表：店铺属于账号，拿错账号的店铺建任务会 422
  const switched = Boolean(runAccountId) && runAccountId !== accountId;
  const scopedShopsQuery = useShops(switched ? runAccountId : null, {
    enabled: switched,
  });
  const scopedShops = scopedShopsQuery.data?.shops;
  const productsQuery = useWarehouseList({
    page: productPage,
    pageSize: 100,
    accountId: runAccountId,
    claimAccountId: runAccountId,
    sort: "savedDesc",
  });
  const products = productsQuery.data?.items ?? [];
  const shopOptions = useMemo(
    () =>
      switched
        ? (scopedShops ?? []).map((item) => ({
            shopId: item.shopId,
            name: item.name,
            site: item.site,
            siteName: item.siteName,
          }))
        : shops,
    [switched, scopedShops, shops]
  );

  // 换了账号后原店铺不属于新账号，自动落到新账号的第一个店铺
  useEffect(() => {
    if (!switched || shopOptions.length === 0) return;
    if (shopOptions.some((item) => item.shopId === shopId)) return;
    setShopId(shopOptions[0].shopId);
  }, [switched, shopOptions, shopId]);

  // 老师多账号：勾选账号时懒加载该账号的店铺。null = 加载中，[] = 已加载无店铺
  const fetchShopsForAccount = async (accId: string): Promise<void> => {
    if (accId in shopsByAccount) return; // 已加载或加载中，不重复请求
    setShopsByAccount((prev) => ({ ...prev, [accId]: null })); // 标记加载中
    try {
      const result = await api.get<{ shops: Shop[] }>(`/shops?accountId=${encodeURIComponent(accId)}`);
      setShopsByAccount((prev) => ({ ...prev, [accId]: result.shops ?? [] }));
    } catch {
      setShopsByAccount((prev) => ({ ...prev, [accId]: [] }));
    }
  };

  /**
   * 勾账号时把该账号下**全部店铺**一起勾上（负责人 2026-11-23：
   * 「第一个勾选上面的时候自动把里面全部的店铺选上」）。
   *
   * 这样「勾账号」= 「这个账号的店全发」，不用再逐个点店铺；
   * 想只发其中几个，勾完再取消掉不要的那几个即可。
   * 取消勾账号则把它名下的店铺选择一起清掉，避免留下孤儿选择 ——
   * 账号不发了、它的店铺却还在选择列表里，提交时会发到不该发的地方。
   */
  const toggleAccount = (accId: string, checked: boolean): void => {
    if (checked) {
      setCheckedAccountIds((prev) => (prev.includes(accId) ? prev : [...prev, accId]));
      void (async () => {
        await fetchShopsForAccount(accId);
        // fetchShopsForAccount 内部已 setState，这里从最新快照取
        setShopsByAccount((prev) => {
          const shops = prev[accId] ?? [];
          if (shops.length > 0) {
            setSelectedShopIdsByAccount((picked) => ({
              ...picked,
              [accId]: shops.map((shop) => shop.shopId),
            }));
          }
          return prev;
        });
      })();
    } else {
      setCheckedAccountIds((prev) => prev.filter((id) => id !== accId));
      setSelectedShopIdsByAccount((prev) => {
        const next = { ...prev };
        delete next[accId];
        return next;
      });
    }
  };

  // 当 checkedAccountIds 变化时，自动加载还没有店铺数据的账号
  useEffect(() => {
    for (const accId of checkedAccountIds) {
      if (!(accId in shopsByAccount)) {
        void fetchShopsForAccount(accId);
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checkedAccountIds]);
  const [errors, setErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!open) return;
    setErrors({});
    // 每次打开抽屉都是一次新的编辑会话，允许重新应用默认模板
    templateTouchedRef.current = false;
    if (task) {
      setDraft({
        name: task.name,
        frequency: (task.frequency as ScheduleFrequency) ?? "daily",
        time: task.time || nowTime(),
        days: task.days,
        dayOfMonth: task.dayOfMonth,
        startDate: task.startDate,
        endDate: task.endDate,
        perRun: task.perRun,
        claimOnly: task.claimOnly,
      });
      setEnabled(task.enabled);
      setShopId(task.shopId);
      // 老任务没有 targets：用 shopId 那一个回填，界面显示为「勾了这一个店铺」
      const taskTargets = task.targets && task.targets.length > 0
        ? task.targets
        : [{ shopId: task.shopId, template: task.template }];
      setSelectedShopIdsByAccount({
        [task.accountId ?? accountId]: taskTargets
          .map((item) => String(item.shopId || ""))
          .filter(Boolean),
      });
      setTemplate(task.template);
      setRunAccountId(task.accountId || accountId);
      setSelectedProductIds(task.selectedProductIds ?? []);
      setPlannedRuns(task.plannedRuns ?? 1);
      return;
    }
    setDraft({
      ...EMPTY_SCHEDULE_FORM,
      time: nowTime(),
      startDate: nowDate(),
    });
    setEnabled(true);
    setSelectedProductIds([]);
    setPlannedRuns(1);
    setProductPage(1);
    setShopId(shops[0]?.shopId ?? "");
    setRunAccountId(accountId);
    // 多账号模式：默认全选所有账号（老师新建任务）
    if (isTeacher && !task && accounts.length > 1) {
      setCheckedAccountIds(accounts.map((a) => a.id));
      // 不预加载——账号渲染后 useEffect 会按需触发
    } else {
      setCheckedAccountIds([]);
    }
    setShopsByAccount({});
    setSelectedShopIdsByAccount({});
    // 模板默认值单独一个 effect（见下），因为它依赖异步回来的 selectedTemplates
  }, [open, task, accountId, shops]); // eslint-disable-line react-hooks/exhaustive-deps

  /**
   * 新建任务时把模板默认值设成「该账号当前的定价模板」。
   *
   * 为什么单独拆一个 effect：`selectedTemplates` 是 `useTemplates()` 的异步
   * 结果，首次打开抽屉时它多半还没回来。原来这条 setTemplate 写在上面那个
   * effect 里、依赖数组又漏了 `selectedTemplates`，于是：
   *   首次打开 → selectedTemplates 是 undefined → 落到 `templates[0]`
   *   数据回来了 → 依赖没变，effect 不重跑 → 模板停在第一套
   * 表现就是「我上次选的模板没被记住，每次都是列表第一套」，
   * 而且这个错值会被直接提交进定时任务。eslint 早就报了这条
   * （react-hooks/exhaustive-deps），只是被当成 warning 忽略了。
   *
   * 依赖用 `preferredTemplate`（字符串）而不是 `selectedTemplates`（对象）：
   * 对象每次 render 都是新引用，放进依赖会让 effect 反复跑、把用户手选的
   * 模板覆盖掉。字符串是值比较，只在真的换了模板时才触发。
   */
  const preferredTemplate = selectedTemplates?.[String(accountId ?? "")] ?? "";

  /**
   * 只列出与已选店铺国家匹配的模板（负责人 2026-11-23）。
   *
   * 「上面勾选了哪个店铺的国家，下面的模板才可以选」—— 马来店只能用马来
   * 模板。之前这个约束在后端 `_template_name_for_site` 里静默纠正（发布时
   * 偷偷换成同站点模板），界面上却把五国模板全列出来让人选错。
   *
   * 一个店铺都没选时不收窄：那时还没有国家可言，列全反而方便先定模板。
   */
  const selectedSites = useMemo(() => {
    const picked = selectedShopIdsByAccount[runAccountId || accountId] ?? [];
    const all = shopOptions;
    const sites = new Set<string>();
    for (const shop of all) {
      if (picked.includes(shop.shopId)) {
        const site = String(shop.site ?? "").trim().toUpperCase();
        if (site) sites.add(site);
      }
    }
    return sites;
  }, [selectedShopIdsByAccount, runAccountId, accountId, shopOptions]);

  const templateOptions = useMemo(() => {
    if (selectedSites.size === 0) return templates;
    const matched = templates.filter((item) => {
      const site = String(
        (item.template as Record<string, unknown> | undefined)?.site ?? ""
      ).trim().toUpperCase();
      return site === "" || selectedSites.has(site);
    });
    // 一个都对不上时不要给空列表：那等于把「模板」这个必填项废掉
    return matched.length > 0 ? matched : templates;
  }, [templates, selectedSites]);

  useEffect(() => {
    if (!open || task) return;
    // 用户已经自己选过了就别再动他——数据晚到时覆盖用户输入比默认值错了更糟
    if (templateTouchedRef.current) return;
    setTemplate(preferredTemplate || templates[0]?.name || "");
  }, [open, task, preferredTemplate, templates]);

  const patch = (next: Partial<ScheduleFormDraft>): void =>
    setDraft((prev) => ({ ...prev, ...next }));

  const validate = (): boolean => {
    const next: Record<string, string> = {};
    if (draft.name.trim().length > LIMITS.scheduleNameLength) {
      next.name = `名称最多 ${LIMITS.scheduleNameLength} 个字符`;
    }
    if (!shopId) next.shopId = "请选择目标店铺";
    if (!task && selectedProductIds.length === 0) next.selectedProductIds = "请至少选择一件仓库商品";
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(draft.time)) next.time = "时间格式应为 HH:MM";
    if (draft.perRun < LIMITS.schedulePerRun.min || draft.perRun > 2000) {
      next.perRun = `每次数量需在 ${LIMITS.schedulePerRun.min}–2000 之间`;
    }
    if (selectedProductIds.length > draft.perRun * plannedRuns) {
      next.selectedProductIds = `已选 ${selectedProductIds.length} 件，计划容量只有 ${draft.perRun * plannedRuns} 件`;
    }
    if (draft.frequency === "weekly" && draft.days.length === 0) {
      next.days = "每周任务至少选一个星期几";
    }
    if (draft.frequency === "monthly" && draft.dayOfMonth.length === 0) {
      next.dayOfMonth = "每月任务至少选一个日期";
    }
    if (draft.frequency === "once" && !draft.startDate) {
      next.startDate = "请选择执行日期";
    }
    // 时间不能早于当前时刻：选了今天且时间已过 → 拒绝。
    // 其他频率同理（今天这一轮已经错过，避免建完立刻显示「已逾期」）。
    if (!next.time && /^([01]\d|2[0-3]):[0-5]\d$/.test(draft.time)) {
      const now = new Date();
      const pad = (value: number): string => String(value).padStart(2, "0");
      const todayIso = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
      const nowHm = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
      const targetDay = draft.frequency === "once" || draft.frequency === "range"
        ? draft.startDate
        : todayIso;
      if (targetDay && targetDay < todayIso) {
        next.startDate = "执行日期不能早于今天";
      } else if (targetDay === todayIso && draft.time < nowHm) {
        next.time = `不能早于当前时间 ${nowHm}`;
      }
    }
    if (draft.frequency === "range") {
      if (!draft.startDate) next.startDate = "请选择开始日期";
      if (!draft.endDate) next.endDate = "请选择结束日期";
      if (draft.startDate && draft.endDate) {
        const start = new Date(`${draft.startDate}T00:00:00`).getTime();
        const end = new Date(`${draft.endDate}T00:00:00`).getTime();
        if (Number.isFinite(start) && Number.isFinite(end)) {
          if (end < start) next.endDate = "结束日期不能早于开始日期";
          else if ((end - start) / 86400000 > LIMITS.scheduleRangeDays) {
            next.endDate = `跨度不能超过 ${LIMITS.scheduleRangeDays} 天`;
          }
        }
      }
    }
    setErrors(next);
    return Object.keys(next).length === 0;
  };

  const submit = (): void => {
    if (!validate()) return;
    const basePayload = {
      name: draft.name.trim() || defaultScheduleName(),
      enabled,
      shopId,
      template,
      frequency: draft.frequency,
      days: draft.frequency === "weekly" ? draft.days : [],
      dayOfMonth: draft.frequency === "monthly" ? draft.dayOfMonth : [],
      startDate: draft.frequency === "range" || draft.frequency === "once"
        ? draft.startDate : "",
      endDate: draft.frequency === "range" ? draft.endDate : "",
      time: draft.time,
      perRun: draft.perRun,
      selectedProductIds,
      plannedRuns: selectedProductIds.length
        ? Math.ceil(selectedProductIds.length / Math.max(1, draft.perRun))
        : null,
      claimOnly: false,
    };

    // 老师多账号模式：同时为多个账号创建任务（每个账号一条）。
    // 每个账号里勾了多个店铺时，任务带 targets 一次发多店。
    if (isTeacher && !task && checkedAccountIds.length > 1) {
      const payloads = checkedAccountIds
        .map((accId) => {
          const targets = targetsForAccount(accId);
          if (targets.length === 0) return null;
          return {
            ...basePayload,
            accountId: accId,
            shopId: targets[0].shopId,
            template: targets[0].template || template,
            targets,
          };
        })
        .filter((item): item is NonNullable<typeof item> => item !== null);
      if (payloads.length === 0) {
        setErrors({ shopId: "所选账号均无可用店铺" });
        return;
      }
      onSubmit(payloads);
      return;
    }

    // 单账号模式：当前账号勾了几个店铺就发几个
    const singles = targetsForAccount(runAccountId || accountId);
    const targets = singles.length > 0 ? singles : [{ shopId, template }];
    onSubmit([{
      ...basePayload,
      accountId: runAccountId || accountId,
      shopId: targets[0].shopId,
      template: targets[0].template || template,
      targets,
    }]);
  };

  const toggleNumber = (list: number[], value: number): number[] =>
    list.includes(value) ? list.filter((item) => item !== value) : [...list, value].sort((a, b) => a - b);

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title={task ? "编辑定时任务" : "新建定时任务"}
      description=""
      closeOnOverlay={false}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button variant="primary" loading={submitting} onClick={submit}>
            {task ? "保存修改" : "创建任务"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="任务名称"  error={errors.name ?? null}>
          <Input
            value={draft.name}
            maxLength={LIMITS.scheduleNameLength}
            placeholder={defaultScheduleName()}
            onChange={(event) => patch({ name: event.currentTarget.value })}
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          {/* 老师新建任务时：多账号复选，可同时为多个账号创建同一套定时任务 */}
          {isTeacher && !task && accounts.length > 1 ? (
            <div className="sm:col-span-2">
              <div className="mb-1 flex items-baseline justify-between gap-1">
                <div className="flex items-baseline gap-1">
                  <span className="text-sm font-medium text-fg">发布账号</span>
                  <span className="text-xs text-fg-secondary">（老师模式：每个勾选账号创建一条独立任务）</span>
                </div>
                <button
                  type="button"
                  className="text-xs text-brand-600 hover:underline"
                  onClick={() => {
                    if (checkedAccountIds.length === accounts.length) {
                      setCheckedAccountIds([]);
                    } else {
                      setCheckedAccountIds(accounts.map((a) => a.id));
                    }
                  }}
                >
                  {checkedAccountIds.length === accounts.length ? "取消全选" : "全选"}
                </button>
              </div>
              <div className="max-h-56 overflow-y-auto rounded-md border border-border-subtle p-2">
                <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
                {accounts.map((account) => {
                  const checked = checkedAccountIds.includes(account.id);
                  const accShopsRaw = shopsByAccount[account.id];
                  // null = 加载中（已触发请求），undefined = 还没触发，[] = 加载完无店铺
                  const accShopsLoading = accShopsRaw === null || (checked && accShopsRaw === undefined);
                  const accShops = accShopsRaw ?? [];
                  const accShopIds = selectedShopIdsByAccount[account.id] ?? [];
                  return (
                    <div key={account.id} className="rounded-md border border-border-subtle bg-canvas-inset/40 p-2">
                      <label className="flex cursor-pointer items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          className="h-4 w-4 rounded border-border accent-brand-500 cursor-pointer"
                          checked={checked}
                          onChange={(e) => toggleAccount(account.id, e.target.checked)}
                        />
                        <span className="truncate font-medium text-fg">{account.name}</span>
                      </label>
                      {checked && accShopsLoading && (
                        <p className="mt-0.5 text-xs text-fg-muted">加载店铺中…</p>
                      )}
                      {checked && !accShopsLoading && accShops.length > 1 && (
                        <div className="mt-2 space-y-1">
                          {accShops.map((shop) => (
                            <label
                              key={shop.shopId}
                              className="flex cursor-pointer items-center gap-2 text-xs"
                            >
                              <input
                                type="checkbox"
                                className="h-3 w-3 cursor-pointer rounded border-border accent-brand-500"
                                checked={accShopIds.includes(shop.shopId)}
                                onChange={(e) => {
                                  setSelectedShopIdsByAccount((prev) => {
                                    const list = prev[account.id] ?? [];
                                    return {
                                      ...prev,
                                      [account.id]: e.target.checked
                                        ? [...list, shop.shopId]
                                        : list.filter((id) => id !== shop.shopId),
                                    };
                                  });
                                }}
                              />
                              {/* 必须带站点：同一账号下常有同名的不同国家店铺，
                                  只显示店名根本分不清该勾哪个。 */}
                              <span className="truncate text-fg-secondary">
                                {shop.name}
                                <span className="ml-1 text-fg-muted">
                                  · {shop.siteName || shop.site}
                                </span>
                              </span>
                            </label>
                          ))}
                        </div>
                      )}
                      {checked && !accShopsLoading && accShops.length === 1 && (
                        <p className="mt-0.5 truncate text-xs text-fg-secondary">→ {accShops[0].name}</p>
                      )}
                      {checked && !accShopsLoading && accShops.length === 0 && (
                        <p className="mt-0.5 text-xs text-danger-600">暂无店铺（未绑定）</p>
                      )}
                    </div>
                  );
                })}
                </div>
              </div>
            </div>
          ) : (
            accounts.length > 1 && (
              <Field label="账号" hint="任务按这个账号执行，切账号不影响它">
                <Select
                  value={runAccountId}
                  onChange={setRunAccountId}
                  options={accounts.map((item) => ({ value: item.id, label: item.name }))}
                  placeholder="选择账号"
                />
              </Field>
            )
          )}

          <Field
            label="目标店铺"
            required
            error={errors.shopId ?? null}
            hint={shopOptions.length > 1 ? "可勾多个店铺，各自套自己站点的模板" : undefined}
          >
            {shopOptions.length > 1 ? (
              <div className="max-h-40 space-y-1 overflow-y-auto rounded-md border border-border-subtle p-2">
                {shopOptions.map((shop) => {
                  const picked = (selectedShopIdsByAccount[runAccountId || accountId] ?? []);
                  const checked = picked.includes(shop.shopId);
                  return (
                    <label
                      key={shop.shopId}
                      className="flex cursor-pointer items-center gap-2 text-xs"
                    >
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5 cursor-pointer rounded border-border accent-brand-500"
                        checked={checked}
                        onChange={(e) => {
                          setSelectedShopIdsByAccount((prev) => {
                            const key = runAccountId || accountId;
                            const list = prev[key] ?? [];
                            return {
                              ...prev,
                              [key]: e.target.checked
                                ? [...list, shop.shopId]
                                : list.filter((id) => id !== shop.shopId),
                            };
                          });
                        }}
                      />
                      <span className="truncate text-fg-secondary">
                        {shop.name}
                        <span className="ml-1 text-fg-muted">
                          · {shop.siteName || shop.site}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
            ) : (
              <Select
                value={shopId}
                onChange={setShopId}
                options={shopOptions.map((shop) => ({
                  value: shop.shopId,
                  label: `${shop.name} · ${shop.siteName || shop.site}`,
                }))}
                placeholder={shopOptions.length === 0 ? "该账号下没有店铺" : "选择店铺"}
                disabled={shopOptions.length === 0}
              />
            )}
          </Field>

          {/*
            定价模板改成勾选开关（负责人 2026-11-23）：
            「发布的时候不是说之前是把这个定价模板差分出来不是下拉吗，
             是勾选开关的形式，上面店铺有哪些国家，下面的模板才允许勾选，
             模板五个一行，之前版本弄过的啊」。

            所以这里不再是下拉：只列出与已勾选店铺的国家对应的模板，
            一行五个，点一下即选中（再点一下取消＝不使用模板）。
            模板本身不可用（invalid）的灰掉不可点。
          */}
          <Field
            label="定价模板"
            error={errors.template ?? null}
          >
            {templateOptions.length === 0 ? (
              <p className="text-sm text-fg-muted">
                {templates.length === 0
                  ? "还没有保存定价模板，先去设置里建一套"
                  : "该国家还没有对应模板"}
              </p>
            ) : (
              <div className="grid grid-cols-5 gap-2">
                {templateOptions.map((item) => {
                  const active = template === item.name;
                  return (
                    <button
                      key={item.name}
                      type="button"
                      disabled={item.invalid}
                      aria-pressed={active}
                      onClick={() => {
                        // 记下「用户自己选过了」：之后默认值晚到也不能覆盖他
                        templateTouchedRef.current = true;
                        setTemplate(active ? "" : item.name);
                      }}
                      className={cn(
                        "flex items-center justify-center gap-1.5 rounded-md border px-2 py-1.5 text-xs transition-colors duration-fast",
                        item.invalid
                          ? "cursor-not-allowed border-border-subtle text-fg-muted line-through"
                          : active
                            ? "border-brand-600 bg-brand-50 font-medium text-brand-700"
                            : "border-border text-fg-secondary hover:border-border-strong hover:bg-surface-hover"
                      )}
                    >
                      {active && <Check className="size-3" />}
                      <span className="truncate">{item.name}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </Field>
        </div>

        <Separator />

        <Field
          label="选择发布商品"
          required
          error={errors.selectedProductIds ?? null}
        >
          <Button
            type="button"
            variant="secondary"
            icon={<PackageCheck className="size-3.5" />}
            onClick={() => setPickerOpen(true)}
          >
            {selectedProductIds.length > 0
              ? `已选 ${selectedProductIds.length} 件，点击修改`
              : "点击选择商品"}
          </Button>
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="频率" required>
            <Select
              value={draft.frequency}
              onChange={(value) => patch({ frequency: value as ScheduleFrequency })}
              options={SCHEDULE_FREQUENCY_OPTIONS.map((option) => ({
                value: option.value,
                label: option.label,
              }))}
            />
          </Field>

          <Field label="执行时间" required error={errors.time ?? null}>
            <TimePicker
              value={draft.time}
              onChange={(next) => patch({ time: next })}
              invalid={Boolean(errors.time)}
              minTime={todayMinTime(draft)}
            />
          </Field>
        </div>

        {draft.frequency === "once" && (
          <Field label="执行日期" required error={errors.startDate ?? null}>
            <DatePicker
              value={draft.startDate}
              onChange={(next) => patch({ startDate: next })}
              placeholder="选择执行日期"
              invalid={Boolean(errors.startDate)}
              minDate={new Date().toISOString().slice(0, 10)}
            />
          </Field>
        )}

        {draft.frequency === "weekly" && (
          <Field label="星期几" required error={errors.days ?? null}>
            <div className="flex flex-wrap gap-2">
              {WEEKDAYS.map((day) => {
                const active = draft.days.includes(day.value);
                return (
                  <button
                    key={day.value}
                    type="button"
                    aria-pressed={active}
                    onClick={() => patch({ days: toggleNumber(draft.days, day.value) })}
                    className={cn(
                      "h-8 rounded-md border px-2.5 text-[13px] font-medium transition-colors duration-fast",
                      active
                        ? "border-brand-600 bg-brand-50 text-brand-700"
                        : "border-border bg-surface text-fg-secondary hover:border-border-strong"
                    )}
                  >
                    {day.label}
                  </button>
                );
              })}
            </div>
          </Field>
        )}

        {draft.frequency === "monthly" && (
          <Field label="每月几号" required error={errors.dayOfMonth ?? null} hint="可多选，1–31 日">
            <div className="flex flex-wrap gap-1.5">
              {Array.from({ length: 31 }).map((_, index) => {
                const value = index + 1;
                const active = draft.dayOfMonth.includes(value);
                return (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={active}
                    onClick={() => patch({ dayOfMonth: toggleNumber(draft.dayOfMonth, value) })}
                    className={cn(
                      "num size-8 rounded-md border text-xs transition-colors duration-fast",
                      active
                        ? "border-brand-600 bg-brand-50 font-medium text-brand-700"
                        : "border-border bg-surface text-fg-secondary hover:border-border-strong"
                    )}
                  >
                    {value}
                  </button>
                );
              })}
            </div>
          </Field>
        )}

        {draft.frequency === "range" && (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="开始日期" required error={errors.startDate ?? null}>
              <DatePicker
                value={draft.startDate}
                onChange={(next) => patch({ startDate: next })}
                placeholder="选择开始日期"
                invalid={Boolean(errors.startDate)}
              />
            </Field>
            <Field label="结束日期" required error={errors.endDate ?? null}>
              <DatePicker
                value={draft.endDate}
                onChange={(next) => patch({ endDate: next })}
                placeholder="选择结束日期"
                invalid={Boolean(errors.endDate)}
                minDate={draft.startDate || undefined}
              />
            </Field>
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field
            label="每次数量"
            required
            error={errors.perRun ?? null}
          >
            <NumberInput
              value={draft.perRun}
              min={LIMITS.schedulePerRun.min}
              max={2000}
              step={1}
              decimals={0}
              suffix="件"
              invalid={Boolean(errors.perRun)}
              onChange={(value) => patch({ perRun: value ?? LIMITS.schedulePerRun.min })}
            />
          </Field>

        </div>


        {Object.keys(errors).length > 0 && (
          <InlineError>有 {Object.keys(errors).length} 项需要修正后再提交。</InlineError>
        )}

      </div>
      <ProductPickerDialog
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        products={products.map((item) => ({
          id: item.id,
          title: item.title || "",
          images: item.images ?? [],
          status: item.status,
          claimed: Boolean(item.claimed),
        }))}
        loading={productsQuery.isLoading}
        selected={pickerSelected}
        onConfirm={(ids) => {
          setPickerSelected(ids);
          setSelectedProductIds(ids);
          setPickerOpen(false);
        }}
        hasMore={productsQuery.data?.hasMore}
        page={productPage}
        onPageChange={setProductPage}
        totalPages={Math.ceil((productsQuery.data?.total ?? 0) / 100)}
      />
    </Dialog>
  );
}

/** 执行历史：每次跑完追加一行，翻得出「昨天跑了几轮、每轮发了几个」。
 *
 *  老版把历史写在 `publish-schedule-log.json` 里；新版此前只留最近一次结果，
 *  出了问题只能去翻服务日志。 */
function ScheduleHistoryPanel() {
  const { data: auth } = useAuthStatus();
  const query = useScheduleLogs({ accountId: auth?.accountId ?? undefined, limit: 200 });
  const rows = query.data?.items ?? [];

  const statusTone = (status: string): "success" | "warning" | "danger" | "neutral" => {
    if (status === "ok") return "success";
    if (status === "partial") return "warning";
    if (status === "failed") return "danger";
    return "neutral";
  };
  const statusLabel = (status: string): string => {
    if (status === "ok") return "已发布";
    if (status === "submitted") return "待回执";
    if (status === "partial") return "部分失败";
    if (status === "failed") return "失败";
    if (status === "empty") return "无商品";
    return status || "-";
  };

  return (
    <div className="px-6 pb-6 pt-3">
      {query.isLoading && (
        <SkeletonTable rows={6} columns={["w-[148px]", "flex-1", "w-[88px]", "w-[88px]", "w-[88px]", "w-[168px]"]} />
      )}

      {query.isError && (
        <Card>
          <ErrorState
            title="执行历史加载失败"
            description={apiErrorMessage(query.error)}
            errorCode={query.error instanceof ApiError ? query.error.code : null}
            onRetry={() => void query.refetch()}
            retrying={query.isFetching}
          />
        </Card>
      )}

      {!query.isLoading && !query.isError && rows.length === 0 && (
        <Card>
          <EmptyState
            icon={<History strokeWidth={1.5} />}
            title="还没有执行记录"
          />
        </Card>
      )}

      {!query.isLoading && !query.isError && rows.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="h-10 bg-canvas-inset/85">
                  <th className="border-b border-border pl-4 pr-3 text-left text-xs font-medium text-fg-muted">
                    时间
                  </th>
                  <th className="border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    任务
                  </th>
                  <th className="border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    结果
                  </th>
                  <th className="w-[76px] border-b border-border px-3 text-right text-xs font-medium text-fg-muted">
                    选中
                  </th>
                  <th className="w-[76px] border-b border-border px-3 text-right text-xs font-medium text-fg-muted">
                    成功
                  </th>
                  <th className="w-[76px] border-b border-border px-3 text-right text-xs font-medium text-fg-muted">
                    待回执
                  </th>
                  <th className="w-[76px] border-b border-border px-3 text-right text-xs font-medium text-fg-muted">
                    失败
                  </th>
                  <th className="border-b border-border px-3 pr-4 text-left text-xs font-medium text-fg-muted">
                    说明
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className="h-11 border-b border-border-subtle last:border-b-0">
                    <td className="whitespace-nowrap pl-4 pr-3 text-fg-muted">
                      {formatRelative(row.at)}
                    </td>
                    <td className="max-w-[220px] truncate px-3 text-fg">{row.taskName || row.taskId}</td>
                    <td className="px-3">
                      <Badge variant={statusTone(row.status)}>{statusLabel(row.status)}</Badge>
                    </td>
                    <td className="px-3 text-right tabular-nums text-fg-muted">{row.selected}</td>
                    <td className="px-3 text-right tabular-nums text-fg-muted">{row.published}</td>
                    <td className="px-3 text-right tabular-nums text-fg-muted">{row.submitted ?? 0}</td>
                    <td className="px-3 text-right tabular-nums text-fg-muted">{row.failed}</td>
                    <td className="max-w-[360px] truncate px-3 pr-4 text-fg-muted" title={row.reason}>
                      {row.reason || "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

export default SchedulesPage;
