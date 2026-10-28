// features/shared —— 图片任务行（公网链接 + 传到图床）
//
// 后端 POST /images/jobs/{id}/upload 早就有了：把已翻译的结果补传到图床，拿 publicUrl。
// 但前端此前没有任何入口——任务 ready 了却只有本机回环地址（previewUrl），
// 导出、复制到平台、发给别人都用不了；上传失败时 uploadError 也没人显示。
//
// 三种行内状态，互斥：
//   · 已有 publicUrl  → 一个小链接（title 给完整地址），点开就是公网图
//   · 没有但 status=ready → 「传到图床」文字按钮（POST /images/jobs/{id}/upload）
//   · uploadError 非空 → 红色提示，title 给完整错误（后端截断到 500 字）
//
// 错误语义按后端分支：409 还没生成完 / 502 上传失败（message 是图床那边的原因）/
// 404 任务不存在。三种都必须把后端 message 原文给出来，否则用户只会反复点。

import { ExternalLink, UploadCloud, AlertTriangle } from "lucide-react";
import { cn, formatDate } from "@/lib/utils";
import { apiErrorMessage } from "@/lib/api";
import { Badge, StatusBadge, toast } from "@/components/ui";
import { TextAction } from "@/features/shared";
import { imageStatus } from "@/features/shared/status";
import { errorCode } from "@/lib/api";
import { useUploadImageJob } from "@/lib/queries";
import type { ImageJob } from "@/lib/types";

export interface ImageJobRowProps {
  job: ImageJob;
  /** 只读模式 / 运行中：不允许发起写操作 */
  disabled?: boolean;
  className?: string;
}

export function ImageJobRow({ job, disabled = false, className }: ImageJobRowProps) {
  const upload = useUploadImageJob();
  const publicUrl = (job.publicUrl ?? "").trim();
  const uploadError = (job.uploadError ?? "").trim();
  const canUpload = !publicUrl && job.status === "ready";

  const runUpload = (): void => {
    // pointer-events-none 只挡鼠标，键盘/脚本仍能触发；写操作在这里再拦一次
    if (disabled) {
      toast.warning("当前不可执行写操作", {
        description: "只读模式下后端会拒绝上传，先在连接面板里切到可写模式",
      });
      return;
    }
    upload.mutate(job.id, {
      onSuccess: (result) => {
        const url = (result.job.publicUrl ?? "").trim();
        toast.success("已传到图床", {
          description: url || "任务已返回公网地址",
        });
      },
      onError: (error) => {
        const message = apiErrorMessage(error);
        // 409：任务还没 ready。文案固定成「任务还没完成，稍后再试」，
        // 后端原文（带当前状态）放 description，比只给一句固定文案更有用
        if (isConflict(error)) {
          toast.warning("任务还没完成，稍后再试", { description: message });
          return;
        }
        // 404：任务不存在（被清理 / 换账号 / 老库）。重试多少次都一样，说清楚
        if (isNotFound(error)) {
          toast.error("这个任务已经不存在", { description: message });
          return;
        }
        // 502：图床那边失败（SFTP/HTTP），原因在后端 message 里，原样展示
        toast.error("传到图床失败", { description: message });
      },
    });
  };

  return (
    <li
      className={cn(
        "flex flex-col gap-1 rounded-md border border-border-subtle px-2.5 py-2",
        className
      )}
    >
      <div className="flex items-center gap-2">
        <StatusBadge meta={imageStatus(job.status)} />
        <span className="min-w-0 flex-1 truncate font-mono text-2xs text-fg-muted">
          {job.source} · {job.id.slice(0, 10)}…
        </span>

        {/* 有公网地址：小链接，title 给完整 URL（地址可能很长，行内放不下） */}
        {publicUrl && (
          <a
            href={publicUrl}
            target="_blank"
            rel="noreferrer noopener"
            title={publicUrl}
            className="inline-flex shrink-0 items-center gap-1 rounded-xs px-1.5 py-0.5 text-2xs font-medium text-brand-700 transition-colors duration-fast hover:bg-brand-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32"
          >
            <ExternalLink className="size-3" strokeWidth={2} />
            公网链接
          </a>
        )}

        {/* 没有公网地址但已经 ready：给一个补传入口 */}
        {canUpload && (
          <span className="shrink-0" title={disabled ? "当前不可执行写操作" : "把结果补传到图床"}>
            <TextAction onClick={runUpload} className={cn("px-1.5 py-0.5", disabled && "pointer-events-none opacity-50")}>
              <span className="inline-flex items-center gap-1">
                <UploadCloud
                  className={cn("size-3", upload.isPending && "animate-pulse")}
                  strokeWidth={2}
                />
                {upload.isPending ? "上传中…" : "传到图床"}
              </span>
            </TextAction>
          </span>
        )}

        <span className="num shrink-0 text-2xs text-fg-muted">
          {formatDate(job.createdAt, "MM-DD HH:mm")}
        </span>
      </div>

      {/* 上传失败原因：后端只截断不抹内容，原样挂出来，title 给全文 */}
      {uploadError && (
        <p
          className="flex items-start gap-1 break-words text-2xs leading-4 text-danger-700"
          title={uploadError}
        >
          <AlertTriangle className="mt-0.5 size-3 shrink-0" strokeWidth={2} />
          <span className="min-w-0 flex-1">上传图床失败：{uploadError}</span>
        </p>
      )}

      {/* ready 但没有地址也没有错误：说明当时没传过（历史任务），按钮已经给了 */}
      {!publicUrl && !uploadError && job.status === "ready" && (
        <p className="text-2xs leading-4 text-fg-subtle">
          这个任务还没有公网地址；上游平台与导出需要公网链接，点「传到图床」补一个。
        </p>
      )}

      {publicUrl && job.uploadedAt ? (
        <p className="text-2xs leading-4 text-fg-subtle">
          已上传 · {formatDate(job.uploadedAt, "MM-DD HH:mm")}
        </p>
      ) : null}
    </li>
  );
}

/** 409 判定：优先看 HTTP 状态，拿不到再看 ApiError.code（CONFLICT_BUSY） */
function isConflict(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  const status = (error as { status?: unknown }).status;
  if (status === 409) return true;
  return errorCode(error) === "CONFLICT_BUSY";
}

/** 404 判定：后端对不存在的 job 抛 NOT_FOUND */
function isNotFound(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  const status = (error as { status?: unknown }).status;
  if (status === 404) return true;
  return errorCode(error) === "NOT_FOUND";
}

/** 列表里的「已传 / 未传」汇总徽标：一眼看出还有几条没有公网地址 */
export function ImageJobUploadSummary({ jobs }: { jobs: ImageJob[] }) {
  const ready = jobs.filter((job) => job.status === "ready");
  if (ready.length === 0) return null;
  const done = ready.filter((job) => (job.publicUrl ?? "").trim()).length;
  const missing = ready.length - done;
  return (
    <Badge variant={missing > 0 ? "warning" : "success"} size="sm">
      公网地址 {done}/{ready.length}
      {missing > 0 ? ` · ${missing} 条待传` : ""}
    </Badge>
  );
}

export default ImageJobRow;
