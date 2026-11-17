// features/automation —— 右侧处理状态与任务摘要
//
// 只展示真实传入的数据，不发起请求、不编造进度。合并为一张紧凑卡片：
// 状态与进度在上，任务摘要在下；所有摘要行固定高度，切换文字/图片开关时
// 只改文字，不改变卡片尺寸。

import { useEffect, useRef, useState } from "react";
import { Activity, RotateCcw } from "lucide-react";
import { cn, formatCount, formatDuration } from "@/lib/utils";
import { Card, CardTitle, Progress, StatusBadge } from "@/components/ui";
import { IconButton } from "@/components/ui";
import {
  languageLabel,
  type AutomationSettings,
  type RunState,
} from "@/features/automation/types";

export interface ProgressPanelProps {
  run: RunState;
  settings: AutomationSettings;
  /** 当前选中件数（勾选集合大小，含跨页）；摘要已移到执行历史，保留入参兼容 */
  selectedCount?: number;
  /** 列表里本页可见件数 */
  visibleCount?: number;
  /** 发布目标店铺名；只拿到 id 时回退显示 id */
  publishShopName?: string;
  /** 任务结束后清空状态面板（返回 idle） */
  onReset?: () => void;
  /** 正在从服务端恢复上次任务记录 */
  recovering?: boolean;
  className?: string;
}

/** 运行中每秒 +1；非运行态返回 null（不显示跳动数字） */
function useTickingElapsed(startedAt: number | null, active: boolean): number | null {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active || startedAt === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, startedAt]);

  if (startedAt === null) return null;
  return Math.max(0, Math.round((now - startedAt) / 1000));
}

export function ProgressPanel({
  run,
  settings,
  onReset,
  recovering = false,
  className,
}: ProgressPanelProps) {
  const running = run.phase === "running";
  // 计时器只在 phase === "running" 时走；任务停止/完成后立即冻结，
  // 不再用 endedAt 是否为 null 判断（hydrateRun 恢复历史任务时 endedAt 可能短暂为 null）。
  const timerActive = running;
  // 文字阶段：正在做什么。文字很快，但用户要求「处理到哪里就显示出来」。
  const textStageHint = !settings.textEnabled
    ? ""
    : run.total > 0 && run.done >= run.total
      ? "已完成"
      : run.done > 0
        ? "处理中"
        : running
          ? "准备中"
          : "";
  // 图片阶段：挑图 → 识别 → 提交翻译 → 写回。
  // 图片是独立后台线程，文字跑完它可能还在跑，必须单独显示当前步骤。
  // 四个明确的状态：等待开始 / 排队中（等并发位）/ 识别中 / 翻译中 / 已完成
  const imageStageHint = !settings.imagesEnabled
    ? ""
    : run.imagePlanned === 0
      ? running ? "识别中…" : ""
      : run.imageDone >= run.imagePlanned
        ? "已翻译完"
        : run.imageDone > 0
          ? "翻译中"
          : "翻译中，等待结果";
  const elapsed = useTickingElapsed(run.startedAt, timerActive);
  const finalElapsed =
    run.startedAt !== null && run.endedAt !== null
      ? Math.max(0, Math.round((run.endedAt - run.startedAt) / 1000))
      : null;
  const shownElapsed = timerActive ? elapsed : finalElapsed;

  // 「已停止」只在真的被用户中断且还有未处理项时才显示。
  // 若 done >= total（所有项都已完成），即使内部 phase 仍是 stopped，
  // 也按「完成」来展示，避免用户看到自相矛盾的「已停止 + 全部完成」。
  const effectivelyDone =
    run.phase === "done" ||
    (run.phase === "stopped" && run.total > 0 && run.done >= run.total);

  const textPercent = settings.textEnabled && run.total > 0 ? (run.done / run.total) * 100 : 0;
  // 图片真正完成的判断：imagePlanned>0 且 imageDone>=imagePlanned，或者无需图片翻译
  const imagesDone = !settings.imagesEnabled
    || (run.imagePlanned > 0 && run.imageDone >= run.imagePlanned)
    || run.imagingComplete === true; // 图片线程明确完成（含提交 0 张的情况）
  // 图片进度：
  //   - idle 态（没有任务）→ 0%
  //   - 已完成态且图片也完成 → 锁定 100%，防止状态重置后显示 0%
  //   - 有提交任务时按完成比例
  //   - 任务已结束且一张都没提交（无需翻译）视为 100%
  const imagePercent = run.phase === "idle"
    ? 0
    : effectivelyDone && settings.imagesEnabled && imagesDone
      ? 100
      : run.imagePlanned > 0
        ? (run.imageDone / run.imagePlanned) * 100
        : !running && run.imagePlanned === 0
          ? 100
          : 0;
  // 批次进度 = 文字和图片都完成才到100：
  //   - idle 态 → 0%
  //   - 已完成态且图片也完成 → 直接 100%
  //   - 两者都开启：平均
  //   - 只开启一项：用那一项
  //   - 都不开启：按件数
  const bothEnabled = settings.textEnabled && settings.imagesEnabled;
  const batchPercent = run.phase === "idle"
    ? 0
    : effectivelyDone && imagesDone
      ? 100
      : bothEnabled
        ? (textPercent + imagePercent) / 2
        : settings.imagesEnabled
          ? imagePercent
          : run.total > 0
            ? (run.done / run.total) * 100
            : 0;

  // 进度条平滑动画：
  //   - 每 50ms 向目标值逼近 1%，让进度条逐格计数（1→2→3…），
  //     而不是一下跳 10-20%（flushSync 并发多件商品时的老问题）。
  //   - 任务新开时（batchPercent 大幅下降），直接重置到 0 再开始计数。
  //   - 目标值只通过 ref 传递，避免 setInterval 闭包引用旧值。
  const TICK_MS = 50;
  const STEP_PCT = 1;
  const [displayBatch, setDisplayBatch] = useState(0);
  const [displayText, setDisplayText] = useState(0);
  const [displayImage, setDisplayImage] = useState(0);
  const prevBatchRef = useRef(batchPercent);
  const displayBatchRef = useRef(0);
  const displayTextRef = useRef(0);
  const displayImageRef = useRef(0);
  const targetBatchRef = useRef(batchPercent);
  const targetTextRef = useRef(textPercent);
  const targetImageRef = useRef(imagePercent);
  // 每次渲染同步目标值（不触发重渲染）
  targetBatchRef.current = batchPercent;
  targetTextRef.current = textPercent;
  targetImageRef.current = imagePercent;
  // 检测新任务：目标大幅下降时立即归零
  useEffect(() => {
    if (batchPercent < prevBatchRef.current - 5) {
      displayBatchRef.current = 0;
      displayTextRef.current = 0;
      displayImageRef.current = 0;
      setDisplayBatch(0);
      setDisplayText(0);
      setDisplayImage(0);
    }
    prevBatchRef.current = batchPercent;
  }, [batchPercent]);
  // 匀速逼近：每 50ms +1%，只涨不降（降只在上面的重置里处理）
  useEffect(() => {
    const timer = window.setInterval(() => {
      const tB = targetBatchRef.current;
      const tT = targetTextRef.current;
      const tI = targetImageRef.current;
      let changed = false;
      if (displayBatchRef.current < tB) {
        displayBatchRef.current = Math.min(tB, displayBatchRef.current + STEP_PCT);
        changed = true;
      } else if (displayBatchRef.current !== tB) {
        displayBatchRef.current = tB;
        changed = true;
      }
      if (displayTextRef.current < tT) {
        displayTextRef.current = Math.min(tT, displayTextRef.current + STEP_PCT);
        changed = true;
      } else if (displayTextRef.current !== tT) {
        displayTextRef.current = tT;
        changed = true;
      }
      if (displayImageRef.current < tI) {
        displayImageRef.current = Math.min(tI, displayImageRef.current + STEP_PCT);
        changed = true;
      } else if (displayImageRef.current !== tI) {
        displayImageRef.current = tI;
        changed = true;
      }
      if (changed) {
        setDisplayBatch(displayBatchRef.current);
        setDisplayText(displayTextRef.current);
        setDisplayImage(displayImageRef.current);
      }
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, []);

  const phaseMeta =
    run.phase === "running"
      ? { label: "运行中", tone: "info" as const, dot: "info" as const, breathe: true }
      : effectivelyDone
        ? run.failed > 0 || run.failures.length > 0 || (run.needsReview ?? 0) > 0
          ? { label: "需检查", tone: "warning" as const, dot: "warning" as const }
          : { label: "已完成", tone: "success" as const, dot: "success" as const }
        : run.phase === "stopped"
          ? { label: "已停止", tone: "warning" as const, dot: "warning" as const }
          : { label: "等待开始", tone: "neutral" as const, dot: "neutral" as const };

  return (
    <Card padding="normal" className={cn("shrink-0 overflow-hidden", className)}>
      {/* 状态 */}
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5">
          <Activity className="size-4 text-fg-muted" strokeWidth={1.75} />
          <CardTitle>处理状态</CardTitle>
        </span>
        <span className="flex items-center gap-1.5">
          {/* 任务结束后显示刷新按钮：点击清空状态、恢复初始界面 */}
          {run.phase !== "idle" && run.phase !== "running" && onReset && (
            <IconButton
              size="sm"
              label="清空记录，恢复初始状态"
              title="清空记录，恢复初始状态"
              icon={<RotateCcw />}
              onClick={onReset}
            />
          )}
          <StatusBadge meta={phaseMeta} />
        </span>
      </div>

      {/* 正在从服务器读取上次任务记录 */}
      {recovering && (
        <p className="mt-2 text-2xs leading-4 text-fg-muted">正在读取上次任务记录…</p>
      )}

      <div className="mt-3 rounded-md border border-border-subtle bg-canvas-inset/45 p-2.5">
        <div className="mt-0 grid grid-cols-2 gap-2">
          <Stat label="批次" value={`${formatCount(run.done)} / ${formatCount(run.total)}`} />
          <Stat label="耗时" value={shownElapsed === null ? "—" : formatDuration(shownElapsed)} />
        </div>
        <Progress className="mt-2" value={displayBatch} label="批次进度" showValue />
      </div>

      {/* 两条真实进度：文字按件，图片按实际提交张数 */}
      <div className="mt-3 grid gap-2.5">
        <div>
          <Progress
            value={displayText}
            indeterminate={settings.textEnabled && running && run.done === 0}
            tone={settings.textEnabled ? "brand" : "warning"}
            label="商品设置与文字"
            showValue
          />
          <p className="num mt-1 h-4 truncate text-2xs leading-4 text-fg-muted">
            {settings.textEnabled
              ? `${formatCount(run.done)} / ${formatCount(run.total)} 件 · ${languageLabel(settings.language)}`
              : "已关闭 · 不翻译标题与属性"}
            {/* 处理到哪一步就说到哪一步（负责人口径 2026-11-12）：
                文字几秒就跑完，但界面不能一直只显示「1/1 件」就没了。
                下面这段把当前正在做的事写出来。 */}
            {textStageHint && ` · ${textStageHint}`}
          </p>
        </div>
        <div>
          <Progress
            value={displayImage}
            indeterminate={settings.imagesEnabled && running && run.imagePlanned === 0}
            tone={settings.imagesEnabled ? "brand" : "warning"}
            label="图片处理"
            showValue
          />
          <p className="num mt-1 h-4 truncate text-2xs leading-4 text-fg-muted">
            {!settings.imagesEnabled
              ? "已关闭 · 不处理图片"
              : effectivelyDone
                ? run.imagePlanned > 0
                  ? `${formatCount(run.imageDone)} / ${formatCount(run.imagePlanned)} 张已翻译 · 已完成`
                  : "已完成"
                : run.phase === "idle"
                  ? "等待开始"
                  : run.imagePlanned === 0
                    ? imageStageHint || "等待识别…"
                    : `${formatCount(run.imageDone)} / ${formatCount(run.imagePlanned)} 张已翻译` +
                      (imageStageHint ? ` · ${imageStageHint}` : "")}
          </p>
        </div>
      </div>

      {/* 失败只留一行入口，明细去「执行历史」看 —— 右侧常驻的卡片
          每次打开都堆满历史错误，运营第一眼看到的是「上次怎么坏」，
          而不是「这次跑到哪」（负责人 2026-11-10 要求精简）。 */}
      {(run.failed > 0 || run.failures.length > 0 || (run.needsReview ?? 0) > 0) && (
        <p className="mt-3 rounded-md border border-warning-600/20 bg-warning-50 px-2.5 py-1.5 text-2xs leading-4 text-warning-700">
          {run.failed > 0
            ? `本批失败 ${formatCount(run.failed)} 件`
            : (run.needsReview ?? 0) > 0
              ? `${formatCount(run.needsReview ?? 0)} 项状态待核对`
              : `本批需检查 ${formatCount(run.failures.length)} 项`}
          {" · 明细见下方执行历史"}
        </p>
      )}
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs leading-4 text-fg-muted">{label}</dt>
      <dd className="num mt-0.5 h-5 truncate text-[13px] font-medium leading-5 text-fg" title={value}>
        {value}
      </dd>
    </div>
  );
}

export default ProgressPanel;
