// features/automation —— 图片服务状态卡（GET /images/verify + POST /images/verify）
//
// 为什么要有这块：老版有 verify_image_api.py，能先看一眼「密钥配没配、从哪个文件读的、
// 是直连还是走代理」，再决定要不要花一次钱做端到端冒烟。新版把这条入口整个丢了，
// 用户配错密钥只能等批量翻译提交后，在某个 job.message 里看到一个 503。
//
// 两个接口分工很明确，界面不能混：
//   · GET  /images/verify —— 只读配置（免费）。回答「配没配、哪来的、打哪个端点」。
//   · POST /images/verify —— 付费冒烟（真发一张 64×64 的图）。回答「这个密钥真的能用吗」。
// 所以付费那条必须过一道 ConfirmDialog：不确认就打，等于替用户偷偷花钱。
//
// 后端的 POST 有个容易踩的语义：**失败也是 200**，上游的失败翻进 body（ok=false +
// message 原文），只有 400（没带 confirm）/ 503（没配密钥）/ 只读模式才走 HTTP 错误。
// 因此下面同时看 HTTP 错误和 body.ok，只判其中一个都会把失败显示成成功。

import { useState } from "react";
import {
  KeyRound,
  PlugZap,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { apiErrorMessage } from "@/lib/api";
import {
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  IconButton,
  InlineError,
  Separator,
  toast,
} from "@/components/ui";
import { useImageVerify, useImageVerifyPaid } from "@/lib/queries";
import type { ImageVerifyPaidResponse } from "@/lib/types";

/**
 * 一张测试图的单价（毫元）。后端在 POST 的 400 details 里给的是 unitMilliyuan:24，
 * 和 image_service.UNIT_MILLIYUAN 同源；这里写死一份是为了在**用户点之前**
 * 就把价格说清楚——那个数只在「没带 confirm 的 400」里返回，正常路径拿不到。
 */
const PAID_TEST_UNIT_MILLIYUAN = 24;

/** 24 毫元 → 「0.024」（毫元 = 千分之一元） */
const PAID_TEST_YUAN = (PAID_TEST_UNIT_MILLIYUAN / 1000).toFixed(3);

function yuanOf(milliyuan: number): string {
  return (Math.max(0, milliyuan) / 1000).toFixed(3);
}

export interface ImageServiceCardProps {
  /** 运行中：付费测试按钮禁用（正在跑的批次不该被一次冒烟打乱额度） */
  disabled?: boolean;
  className?: string;
}

export function ImageServiceCard({ disabled = false, className }: ImageServiceCardProps) {
  const verify = useImageVerify();
  // 只读检查不受「运行中」影响：它不花钱、也不改任何状态
  const paid = useImageVerifyPaid();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [setupOpen, setSetupOpen] = useState(false);
  // 上一次冒烟的结果：toast 会消失，这里留住 requestId 供排查时对齐日志
  const [lastResult, setLastResult] = useState<ImageVerifyPaidResponse | null>(null);

  const data = verify.data;
  const configured = data?.configured === true;
  const modeLabel = data?.mode === "proxy" ? "代理" : data?.mode === "direct" ? "直连" : "未知";

  const runPaid = (): void => {
    paid.mutate(undefined, {
      onSuccess: (result) => {
        setLastResult(result);
        setConfirmOpen(false);
        if (result.ok) {
          toast.success("图片服务连通性正常", {
            description: `请求号 ${result.requestId}${result.taskId ? ` · 任务 ${result.taskId}` : ""} · ${result.message}`,
          });
          return;
        }
        // ok=false 也是 200：后端把上游的失败原文放在 message 里，原样展示，不吞
        toast.error("付费测试未通过", {
          description: `请求号 ${result.requestId} · ${result.message}`,
        });
      },
      // 400（没带 confirm）/ 503（没配密钥）/ 只读模式走这里，message 是后端原文
      onError: (error) => {
        setConfirmOpen(false);
        toast.error("付费测试没有发出去", { description: apiErrorMessage(error) });
      },
    });
  };

  return (
    <>
      <div
        className={cn(
          "flex flex-col gap-2 rounded-md border border-border bg-canvas-inset/40 px-3 py-2.5",
          className
        )}
      >
        {/* 标题行：状态 + 指纹 + 两个动作 */}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="flex min-w-0 flex-wrap items-center gap-1.5">
            <KeyRound className="size-3.5 shrink-0 text-fg-muted" strokeWidth={1.75} />
            <span className="shrink-0 whitespace-nowrap text-sm font-medium leading-[18px] text-fg-secondary">
              图片服务
            </span>
            {verify.isLoading && (
              <Badge variant="neutral" size="sm">
                检查中…
              </Badge>
            )}
            {!verify.isLoading && !verify.isError && configured && (
              <Badge variant="success" size="sm" icon={<ShieldCheck className="size-3" />}>
                已配置
              </Badge>
            )}
            {!verify.isLoading && !verify.isError && !configured && (
              <Badge variant="warning" size="sm" icon={<ShieldAlert className="size-3" />}>
                未配置
              </Badge>
            )}
            {configured && data?.keyHint && (
              <span
                className="shrink-0 rounded-xs bg-canvas-inset px-1.5 py-0.5 font-mono text-2xs leading-4 text-fg-muted"
                title="只显示前后各 4 位，完整密钥不会出现在界面上"
              >
                {data.keyHint}
              </span>
            )}
            {!verify.isLoading && !verify.isError && !configured && (
              <Button size="sm" variant="ghost" onClick={() => setSetupOpen(true)}>
                去设置
              </Button>
            )}
          </span>

          <span className="flex shrink-0 items-center gap-1.5">
            <IconButton
              label="重新检查图片服务配置"
              size="sm"
              variant="ghost"
              icon={<RefreshCw className={cn(verify.isFetching && "animate-spin")} />}
              disabled={verify.isFetching}
              onClick={() => void verify.refetch()}
            />
            <span
              className="inline-flex"
              title={
                disabled
                  ? "正在处理批次，先等这一批跑完"
                  : !configured
                    ? "密钥还没配好：点了会由后端返回「密钥未配置」的说明，不会产生费用"
                    : "真发一张 64×64 的测试图，验证密钥端到端可用"
              }
            >
              <Button
                size="sm"
                variant="secondary"
                icon={<PlugZap className="size-3.5" />}
                // 未配置时不拦：后端会回 503 并带上「该把密钥放哪」的说明，
                // 让用户看到原文比把按钮灰掉、只留一句 title 更有用（也不会产生费用）
                disabled={disabled || paid.isPending}
                loading={paid.isPending}
                loadingText="测试中…"
                onClick={() => setConfirmOpen(true)}
              >
                测试连通性
              </Button>
            </span>
          </span>
        </div>

        {verify.isError && (
          <InlineError
            onRetry={() => void verify.refetch()}
            retryLabel={verify.isFetching ? "重试中…" : "重试"}
          >
            {apiErrorMessage(verify.error)}
          </InlineError>
        )}

        {verify.isLoading && (
          <p className="text-2xs leading-4 text-fg-muted">正在检查图片服务配置…</p>
        )}

        {!verify.isLoading && !verify.isError && data && (
          <>
            <div className="flex flex-col gap-1 text-xs leading-[18px]">
              <p className="flex flex-wrap items-center gap-1.5">
                <span className="shrink-0 text-fg-muted">模式</span>
                <Badge variant={data.mode === "proxy" ? "info" : "neutral"} size="sm">
                  {modeLabel}
                </Badge>
                <span className="shrink-0 text-fg-muted">· 端点</span>
                <span
                  className="min-w-0 truncate font-mono text-2xs text-fg-secondary"
                  title={data.endpoint}
                >
                  {data.endpoint || "—"}
                </span>
              </p>
              {/* 密钥来源：这句专门回答「我明明没填过，怎么会有密钥」 */}
              <p className="flex flex-wrap items-center gap-1.5">
                <span className="shrink-0 text-fg-muted">密钥来源</span>
                <span className="min-w-0 break-all text-fg-secondary" title={data.path || undefined}>
                  {data.source || "（未读到）"}
                </span>
              </p>
              {data.path && (
                <p className="min-w-0 break-all font-mono text-2xs leading-4 text-fg-muted">
                  {data.path}
                </p>
              )}
            </div>

            {/* message 全文：未配置时它是「文件放哪、叫什么名字」的可行动说明，
                已配置时是「要不要做付费验证」的提示。截断等于把说明书撕一半。 */}
            {data.message && (
              <p
                className={cn(
                  "rounded-md border px-2.5 py-1.5 text-2xs leading-4",
                  configured
                    ? "border-border-subtle bg-surface text-fg-muted"
                    : "border-warning-600/20 bg-warning-50 text-warning-700"
                )}
              >
                {data.message}
              </p>
            )}
          </>
        )}

        {/* 上一次冒烟的结果：请求号留着，排查「谁在扣费」时能和日志对齐 */}
        {lastResult && (
          <>
            <Separator className="my-0.5" />
            <div className="flex flex-col gap-1 text-2xs leading-4">
              <p className="flex flex-wrap items-center gap-1.5">
                <Badge variant={lastResult.ok ? "success" : "danger"} size="sm">
                  {lastResult.ok ? "测试通过" : "测试未通过"}
                </Badge>
                <span className="text-fg-muted">状态 {lastResult.status || "unknown"}</span>
                <span className="text-fg-muted">
                  · 占用 {yuanOf(lastResult.reservedMilliyuan)} 元
                </span>
                <span className="font-mono text-fg-muted" title={lastResult.requestId}>
                  请求号 {lastResult.requestId}
                </span>
              </p>
              <p
                className={cn(
                  "break-words",
                  lastResult.ok ? "text-fg-muted" : "text-danger-700"
                )}
              >
                {lastResult.message || "（后端没有返回说明）"}
              </p>
            </div>
          </>
        )}
      </div>

      {/* 二次确认：付费动作不能一点就走。
          三句话必须都在：这是付费测试 / 会真实提交一张图片并可能产生费用 / 约 0.024 元。 */}
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={(open) => {
          if (!paid.isPending) setConfirmOpen(open);
        }}
        title="这是付费测试"
        description={`会真实提交一张图片并可能产生费用，约 ${PAID_TEST_YUAN} 元。`}
        confirmLabel="开始测试"
        cancelLabel="取消"
        loading={paid.isPending}
        onConfirm={runPaid}
      >
        <div className="flex flex-col gap-2 text-[13px] leading-5 text-fg-muted">
          <p>
            会向图片服务真实提交一张 64×64 的测试图并可能产生费用，约{" "}
            <span className="num font-medium text-fg">{PAID_TEST_YUAN} 元</span>
            （每张 {PAID_TEST_UNIT_MILLIYUAN} 毫元）。这是确认密钥端到端可用的唯一办法。
          </p>
          <p>
            无论成功还是失败，这次调用都会占用一次额度，且不可撤销——失败也说明钱花掉了，
            只是上游没接住。
          </p>
          {data?.endpoint && (
            <p className="break-all font-mono text-2xs leading-4 text-fg-subtle">
              请求端点：{data.endpoint}
            </p>
          )}
          {data?.keyHint && (
            <p className="text-2xs leading-4 text-fg-subtle">
              将使用密钥 {data.keyHint}
              {data.source ? `（来源：${data.source}）` : ""}
            </p>
          )}
        </div>
      </ConfirmDialog>

      {/* 未配置时的「去设置」：新版没有独立的密钥设置页，这里把后端给的可行动说明
          摊开（文件路径 / 环境变量 / 老版遗留位置），并给一个复制路径的动作。 */}
      <Dialog
        open={setupOpen}
        onOpenChange={setSetupOpen}
        size="md"
        title="配置图片翻译密钥"
        description="密钥是本地文件或环境变量，配好后重启应用，再点「重新检查」。"
        footer={
          <>
            {data?.path && (
              <Button
                variant="ghost"
                onClick={() => {
                  const text = data.path;
                  if (!navigator.clipboard) {
                    toast.error("当前环境不支持剪贴板，请手动选中路径复制");
                    return;
                  }
                  navigator.clipboard.writeText(text).then(
                    () => toast.success("已复制密钥文件路径"),
                    () => toast.error("复制失败，请手动选中路径复制")
                  );
                }}
              >
                复制路径
              </Button>
            )}
            <Button variant="primary" onClick={() => setSetupOpen(false)}>
              知道了
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3 text-[13px] leading-5">
          <p className="whitespace-pre-line break-words text-fg-secondary">
            {data?.message || "后端没有返回配置说明，请检查服务端日志。"}
          </p>
          {data?.path && (
            <p className="break-all rounded-md border border-border bg-canvas-inset px-2.5 py-1.5 font-mono text-2xs leading-4 text-fg-muted">
              {data.path}
            </p>
          )}
          <p className="text-2xs leading-4 text-fg-subtle">
            配好后回来点「重新检查」：configured 变绿、模式显示为「直连」或「代理」，
            再点「测试连通性」做一次付费冒烟。
          </p>
        </div>
      </Dialog>
    </>
  );
}

export default ImageServiceCard;
