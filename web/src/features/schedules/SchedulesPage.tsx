// features/schedules —— 定时发布（api.md §9）
// 列表：启用开关、频率、时间、每次数量、店铺、下次执行、最近结果。
// 新建/编辑：名称、频率（daily/weekly/monthly/range）、时间、星期几、起止日期、每次数量、店铺、模板。
// 删除确认 + 立即执行（演练）。

import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  CalendarClock,
  CalendarDays,
  History,
  Play,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { cn, formatCount, formatRelative } from "@/lib/utils";
import { ApiError, apiErrorMessage } from "@/lib/api";
import {
  LIMITS,
  SCHEDULE_FREQUENCY_OPTIONS,
  SITE_LANGUAGES,
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
} from "@/lib/queries";
import type { Schedule, ScheduleFrequency } from "@/lib/types";
import { EMPTY_SCHEDULE_FORM, type ScheduleFormDraft } from "@/features/stores";
import { ConnectHintBar } from "@/features/shared";
import { useConnectionState } from "@/features/auth/guard";
import { PageContent, PageHeader } from "@/components/layout";
import { Tabs, tabId, tabPanelId } from "@/components/ui";
import { PublishPage } from "@/features/publish/PublishPage";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  Drawer,
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
export function SchedulesPage({ tab: initialTab }: { tab?: "schedules" | "logs" } = {}) {
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
            : result.status === "partial"
              ? "部分完成"
              : result.status === "empty"
                ? "没有可处理的商品"
                : "执行失败";
        const options = {
          description:
            result.status === "empty"
              ? result.reason || "仓库里没有可处理的商品"
              : `计划 ${result.requested} 件 · 成功 ${result.count} 件 · 失败 ${result.failed} 件${
                  result.reason ? ` · ${result.reason}` : ""
                }`,
        };
        if (result.status === "ok") toast.success(label, options);
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

  const [tab, setTab] = useState<"schedules" | "logs" | "history">(initialTab ?? "schedules");

  if (tab === "logs") {
    return (
      <>
        <PageHeader
          title="定时发布"
          description="定时任务与发布回执"
        />
        <PageContent width="table" padding="p-0" scroll={false}>
          <div className="border-b border-border-subtle px-6 pt-3">
            <Tabs
              value={tab}
              onChange={(value) => setTab(value as "schedules" | "logs" | "history")}
              bordered={false}
              items={[
                { value: "schedules", label: "定时任务" },
                { value: "logs", label: "发布记录" },
                { value: "history", label: "执行历史" },
              ]}
            />
          </div>
          <div
            role="tabpanel"
            id={tabPanelId("schedules", "logs")}
            aria-labelledby={tabId("schedules", "logs")}
            className="contents"
          >
            <PublishPage embedded />
          </div>
        </PageContent>
      </>
    );
  }

  if (tab === "history") {
    return (
      <>
        <PageHeader
          title="定时发布"
          description="每一轮执行的结果，新的在前"
        />
        <PageContent width="table" padding="p-0" scroll={false}>
          <div className="border-b border-border-subtle px-6 pt-3">
            <Tabs
              value={tab}
              onChange={(value) => setTab(value as "schedules" | "logs" | "history")}
              bordered={false}
              items={[
                { value: "schedules", label: "定时任务" },
                { value: "logs", label: "发布记录" },
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
        description={schedulesQuery.data ? `共 ${formatCount(schedulesQuery.data.total)} 个任务` : undefined}
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
            onChange={(value) => setTab(value as "schedules" | "logs" | "history")}
            bordered={false}
            idBase="schedules"
            items={[
              { value: "schedules", label: "定时任务" },
              { value: "logs", label: "发布记录" },
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
              description="定时任务会按频率把仓库里未上架的商品自动发布到指定店铺。"
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
              description="创建任务后，系统会按设定的频率自动把仓库商品发布到指定店铺。"
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
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="h-10 bg-canvas-inset/85 backdrop-blur-[2px]">
                    <th className="sticky top-0 w-[64px] border-b border-border pl-4 text-left text-xs font-medium text-fg-muted">
                      启用
                    </th>
                    <th className="sticky top-0 border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      任务
                    </th>
                    <th className="sticky top-0 w-[148px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      频率 / 时间
                    </th>
                    <th className="sticky top-0 w-[88px] border-b border-border px-3 text-right text-xs font-medium text-fg-muted">
                      每次
                    </th>
                    <th className="sticky top-0 w-[152px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      店铺 / 模板
                    </th>
                    <th className="sticky top-0 w-[168px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                      下次执行
                    </th>
                    <th className="sticky top-0 w-[176px] border-b border-border px-3 pr-4 text-right text-xs font-medium text-fg-muted">
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
                          {task.id}
                          {task.claimOnly ? " · 仅认领" : ""}
                        </p>
                      </td>
                      <td className="px-3 py-2.5">
                        <p className="text-[13px] text-fg">{frequencyText(task)}</p>
                        <p className="num text-2xs text-fg-muted">{task.time}</p>
                      </td>
                      <td className="px-3 py-2.5 text-right">
                        <span className="num text-[13px] text-fg">{formatCount(task.perRun)}</span>
                      </td>
                      <td className="px-3 py-2.5">
                        <p className="truncate text-[13px] text-fg">{task.shopName || task.shopId}</p>
                        <p className="truncate text-2xs text-fg-muted">
                          {task.template || "未设模板"}
                        </p>
                      </td>
                      <td className="px-3 py-2.5">
                        <p className="text-[13px] text-fg">
                          {task.enabled ? task.nextDueText || "—" : "已停用"}
                        </p>
                        {/* lastResult.status 为空（从未跑过）时不渲染「上次」行，避免出现孤零零的 0/20 */}
                        {task.lastResult?.status && (
                          <p className="flex items-center gap-1 text-2xs text-fg-muted">
                            <span>上次</span>
                            <StatusBadge
                              meta={
                                task.lastResult.status === "ok"
                                  ? { label: "成功", tone: "success", dot: "success" }
                                  : task.lastResult.status === "partial"
                                    ? { label: "部分", tone: "warning", dot: "warning" }
                                    : task.lastResult.status === "empty"
                                      ? { label: "无商品", tone: "neutral", dot: "muted" }
                                      : { label: "失败", tone: "danger", dot: "danger" }
                              }
                            />
                            <span className="num">
                              {task.lastResult.count}/{task.perRun}
                            </span>
                            <span className="text-fg-muted">{formatRelative(task.lastResult.at)}</span>
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
        onSubmit={(payload) => {
          if (editing) {
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
            if (payload.claimOnly !== editing.claimOnly) patch.claimOnly = payload.claimOnly;
            if (JSON.stringify(payload.days) !== JSON.stringify(editing.days)) patch.days = payload.days;
            if (JSON.stringify(payload.dayOfMonth) !== JSON.stringify(editing.dayOfMonth)) {
              patch.dayOfMonth = payload.dayOfMonth;
            }
            if (payload.startDate !== editing.startDate) patch.startDate = payload.startDate;
            if (payload.endDate !== editing.endDate) patch.endDate = payload.endDate;

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
          createSchedule.mutate(payload, {
            onSuccess: () => {
              toast.success("已创建定时任务");
              setFormOpen(false);
            },
            onError: (error) => toast.error("创建失败", { description: apiErrorMessage(error) }),
          });
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

interface ScheduleFormDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  task: Schedule | null;
  accountId: string;
  /** 已连接的账号，用来给任务选「用哪个账号跑」（老版 schedule-account） */
  accounts: Array<{ id: string; name: string }>;
  shops: Array<{ shopId: string; name: string; site: string; siteName: string }>;
  templates: Array<{ name: string; invalid?: boolean }>;
  /** 账号 → 当前模板名；新建任务时用它当默认值 */
  selectedTemplates?: Record<string, string>;
  submitting: boolean;
  onSubmit: (payload: {
    name: string;
    enabled: boolean;
    accountId: string;
    shopId: string;
    template: string;
    frequency: ScheduleFrequency;
    days: number[];
    dayOfMonth: number[];
    startDate: string;
    endDate: string;
    time: string;
    perRun: number;
    claimOnly: boolean;
  }) => void;
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

  // 切了账号就要换店铺列表：店铺属于账号，拿错账号的店铺建任务会 422
  const switched = Boolean(runAccountId) && runAccountId !== accountId;
  const scopedShopsQuery = useShops(switched ? runAccountId : null, {
    enabled: switched,
  });
  const scopedShops = scopedShopsQuery.data?.shops;
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
        time: task.time || "09:30",
        days: task.days,
        dayOfMonth: task.dayOfMonth,
        startDate: task.startDate,
        endDate: task.endDate,
        perRun: task.perRun,
        claimOnly: task.claimOnly,
      });
      setEnabled(task.enabled);
      setShopId(task.shopId);
      setTemplate(task.template);
      setRunAccountId(task.accountId || accountId);
      return;
    }
    setDraft(EMPTY_SCHEDULE_FORM);
    setEnabled(true);
    setShopId(shops[0]?.shopId ?? "");
    setRunAccountId(accountId);
    // 模板默认值单独一个 effect（见下），因为它依赖异步回来的 selectedTemplates
  }, [open, task, accountId, shops]);

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
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(draft.time)) next.time = "时间格式应为 HH:MM";
    if (draft.perRun < LIMITS.schedulePerRun.min || draft.perRun > LIMITS.schedulePerRun.max) {
      next.perRun = `每次数量需在 ${LIMITS.schedulePerRun.min}–${LIMITS.schedulePerRun.max} 之间`;
    }
    if (draft.frequency === "weekly" && draft.days.length === 0) {
      next.days = "每周任务至少选一个星期几";
    }
    if (draft.frequency === "monthly" && draft.dayOfMonth.length === 0) {
      next.dayOfMonth = "每月任务至少选一个日期";
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
    onSubmit({
      name: draft.name.trim() || `定时发布 ${draft.time}`,
      enabled,
      accountId: runAccountId || accountId,
      shopId,
      template,
      frequency: draft.frequency,
      days: draft.frequency === "weekly" ? draft.days : [],
      dayOfMonth: draft.frequency === "monthly" ? draft.dayOfMonth : [],
      startDate: draft.frequency === "range" ? draft.startDate : "",
      endDate: draft.frequency === "range" ? draft.endDate : "",
      time: draft.time,
      perRun: draft.perRun,
      claimOnly: draft.claimOnly,
    });
  };

  const toggleNumber = (list: number[], value: number): number[] =>
    list.includes(value) ? list.filter((item) => item !== value) : [...list, value].sort((a, b) => a - b);

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title={task ? "编辑定时任务" : "新建定时任务"}
      description={task ? task.id : "按频率把仓库里未上架的商品自动发布到指定店铺"}
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
        <Field label="任务名称" hint={`留空自动生成；最多 ${LIMITS.scheduleNameLength} 个字符`} error={errors.name ?? null}>
          <Input
            value={draft.name}
            maxLength={LIMITS.scheduleNameLength}
            placeholder={`定时发布 ${draft.time}`}
            onChange={(event) => patch({ name: event.currentTarget.value })}
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          {accounts.length > 1 && (
            <Field label="账号" hint="任务按这个账号执行，切账号不影响它">
              <Select
                value={runAccountId}
                onChange={setRunAccountId}
                options={accounts.map((item) => ({ value: item.id, label: item.name }))}
                placeholder="选择账号"
              />
            </Field>
          )}

          <Field label="目标店铺" required error={errors.shopId ?? null}>
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
          </Field>

          <Field label="定价模板" hint={templates.length === 0 ? "还没有保存定价模板" : undefined}>
            <Select
              value={template}
              onChange={(next) => {
                // 记下「用户自己选过了」：之后即使 selectedTemplates 才回来，
                // 也不能覆盖他——默认值错只是不方便，覆盖用户输入是丢数据。
                templateTouchedRef.current = true;
                setTemplate(next);
              }}
              options={templates.map((item) => ({
                value: item.name,
                label: item.invalid ? `${item.name}（不可用）` : item.name,
                disabled: item.invalid,
              }))}
              placeholder="不使用模板"
            />
          </Field>
        </div>

        <Separator />

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

          <Field label="执行时间" required error={errors.time ?? null} hint="本地时区，HH:MM">
            <Input
              value={draft.time}
              inputMode="numeric"
              placeholder="09:30"
              numeric
              invalid={Boolean(errors.time)}
              onChange={(event) => patch({ time: event.currentTarget.value })}
            />
          </Field>
        </div>

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
              <Input
                type="date"
                value={draft.startDate}
                invalid={Boolean(errors.startDate)}
                onChange={(event) => patch({ startDate: event.currentTarget.value })}
              />
            </Field>
            <Field
              label="结束日期"
              required
              error={errors.endDate ?? null}
              hint={`跨度 ≤ ${LIMITS.scheduleRangeDays} 天`}
            >
              <Input
                type="date"
                value={draft.endDate}
                invalid={Boolean(errors.endDate)}
                onChange={(event) => patch({ endDate: event.currentTarget.value })}
              />
            </Field>
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field
            label="每次数量"
            required
            error={errors.perRun ?? null}
            hint={`${LIMITS.schedulePerRun.min}–${LIMITS.schedulePerRun.max} 件`}
          >
            <NumberInput
              value={draft.perRun}
              min={LIMITS.schedulePerRun.min}
              max={LIMITS.schedulePerRun.max}
              step={1}
              decimals={0}
              suffix="件"
              invalid={Boolean(errors.perRun)}
              onChange={(value) => patch({ perRun: value ?? LIMITS.schedulePerRun.min })}
            />
          </Field>

          <div className="flex flex-col gap-2 pt-6">
            <Switch checked={enabled} onChange={setEnabled} label="创建后立即启用" />
            <Checkbox
              checked={draft.claimOnly}
              onChange={(checked) => patch({ claimOnly: checked })}
              label="只认领进采集箱，不上架"
            />
          </div>
        </div>

        {Object.keys(errors).length > 0 && (
          <InlineError>有 {Object.keys(errors).length} 项需要修正后再提交。</InlineError>
        )}

        <div className="rounded-md border border-border-subtle bg-canvas-inset/60 p-3 text-xs leading-[18px] text-fg-muted">
          <p className="flex items-center gap-1.5 font-medium text-fg-secondary">
            <CalendarDays className="size-3.5" strokeWidth={2} />
            执行说明
          </p>
          <p className="mt-1">
            任务会挑选该账号下「未发布」的仓库商品，按入库时间倒序取每轮数量；不足时不报错，只记一次
            <span className="text-fg-secondary"> 部分完成</span>。
          </p>
          <p className="mt-1">
            站点语言参考：
            {Object.entries(SITE_LANGUAGES)
              .map(([site, languages]) => `${site}=${languages.join("/")}`)
              .join("，")}
          </p>
        </div>
      </div>
    </Drawer>
  );
}

/** 执行历史：每次跑完追加一行，翻得出「昨天跑了几轮、每轮发了几个」。
 *
 *  老版把历史写在 `publish-schedule-log.json` 里；新版此前只留最近一次结果，
 *  出了问题只能去翻服务日志。 */
function ScheduleHistoryPanel() {
  const query = useScheduleLogs({ limit: 200 });
  const rows = query.data?.items ?? [];

  const statusTone = (status: string): "success" | "warning" | "danger" | "neutral" => {
    if (status === "ok") return "success";
    if (status === "partial") return "warning";
    if (status === "failed") return "danger";
    return "neutral";
  };
  const statusLabel = (status: string): string => {
    if (status === "ok") return "已发布";
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
            description="定时任务每跑一轮就会在这里留一条记录，能看到选中几个、成功几个、失败原因。"
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
