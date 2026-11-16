// 自动更新弹窗：检测到新版本时自动弹出，自动开始下载，下载完自动安装重启。
// 只在 Electron 桌面端（window.desktop?.update 存在）走软件内更新；
// 浏览器环境退回「查看更新」跳到设置页。

import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { toast } from "@/components/ui/toast";
import { Button } from "@/components/ui/button";
import type { VersionPayload } from "@/lib/queries";

interface UpdateDialogProps {
  version: VersionPayload | null | undefined;
  onViewSettings: () => void;
}

/** 对话框主体：覆盖层 + 卡片 */
export function UpdateDialog({ version, onViewSettings }: UpdateDialogProps) {
  const [open, setOpen] = useState(false);
  const notifiedRef = useRef<string | null>(null);

  // 检测到新版本时自动弹出（每个版本只弹一次）
  useEffect(() => {
    if (!version?.ok || !version.updateAvailable || !version.latest) return;
    if (notifiedRef.current === version.latest) return;
    notifiedRef.current = version.latest;
    setOpen(true);
  }, [version]);

  if (!open || !version?.updateAvailable) return null;

  return (
    <UpdateDialogContent
      version={version}
      onClose={() => setOpen(false)}
      onViewSettings={onViewSettings}
    />
  );
}

function UpdateDialogContent({
  version,
  onClose,
  onViewSettings,
}: {
  version: VersionPayload;
  onClose: () => void;
  onViewSettings: () => void;
}) {
  const desktopUpdate = typeof window !== "undefined" ? window.desktop?.update : undefined;
  const hasDesktop = Boolean(desktopUpdate);

  const [phase, setPhase] = useState<"idle" | "downloading" | "done" | "error">("idle");
  const [progress, setProgress] = useState(0);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const autoStartedRef = useRef(false);

  // 订阅进度
  useEffect(() => {
    if (!desktopUpdate) return;
    return desktopUpdate.onProgress((p: { received: number; total: number; percent: number }) => {
      setProgress(Math.round(p.percent));
    });
  }, [desktopUpdate]);

  const startDownload = (): void => {
    if (!desktopUpdate) return;
    const manifest = {
      version: String(version.latest || ""),
      url: String(version.url || ""),
      sha256: String(version.sha256 || ""),
      sizeBytes: version.sizeBytes ?? undefined,
      notes: version.notes ?? undefined,
    };
    if (!manifest.url || !manifest.sha256) {
      setErrorMsg("更新清单缺少下载地址，请稍后再试");
      return;
    }
    setPhase("downloading");
    setProgress(0);
    setErrorMsg(null);
    void desktopUpdate.download(manifest).then((result: { ok: boolean; error?: string }) => {
      if (!result.ok) {
        setPhase("error");
        setErrorMsg(result.error || "下载失败，请稍后重试");
        return;
      }
      setPhase("done");
    });
  };

  const handleInstall = (): void => {
    if (!desktopUpdate) return;
    void desktopUpdate.install().then((result: { ok: boolean; error?: string }) => {
      if (!result.ok) {
        setErrorMsg(result.error || "安装失败");
        toast.error("安装失败", { description: result.error });
        return;
      }
    });
  };

  // 检查是否已有缓存包 → 自动安装；否则自动开始下载
  useEffect(() => {
    if (!desktopUpdate || autoStartedRef.current) return;
    autoStartedRef.current = true;
    void desktopUpdate.pending().then((p: { path: string; version: string } | null) => {
      if (p && p.version === version.latest) {
        setPhase("done");
        // 已有安装包，自动安装
        handleInstall();
      } else {
        // 没有缓存，自动开始下载
        startDownload();
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [desktopUpdate]);

  // 下载完成后自动安装
  useEffect(() => {
    if (phase === "done") {
      handleInstall();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  return (
    <div
      className="fixed inset-0 z-[200] flex items-center justify-center bg-overlay backdrop-blur-[2px]"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="mx-4 w-full max-w-sm rounded-xl border border-border bg-surface p-6 shadow-2xl">
        {/* 标题 */}
        <div className="flex items-start gap-3">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-brand-100 text-brand-600">
            <RefreshCw className="size-5" strokeWidth={2} />
          </div>
          <div className="min-w-0">
            <p className="font-semibold text-fg">发现新版本 {version.latest}</p>
            <p className="text-sm text-fg-muted">
              {phase === "downloading"
                ? "正在下载更新…"
                : phase === "done"
                  ? "下载完成，准备安装重启…"
                  : phase === "error"
                    ? "下载失败"
                    : "正在准备更新…"}
            </p>
          </div>
        </div>

        {/* 更新说明 */}
        {version.notes && (
          <p className="mt-4 rounded-md bg-canvas-inset px-3 py-2 text-sm text-fg-muted">
            {version.notes}
          </p>
        )}

        {/* 下载进度 */}
        {(phase === "downloading" || phase === "done") && (
          <div className="mt-4">
            <div className="flex justify-between text-xs text-fg-muted">
              <span>{phase === "done" ? "下载完成" : "正在下载"}</span>
              <span>{phase === "done" ? "100" : progress}%</span>
            </div>
            <div className="mt-1 h-2 overflow-hidden rounded-full bg-bg-subtle">
              <div
                className="h-full rounded-full bg-brand-600 transition-all duration-300"
                style={{ width: phase === "done" ? "100%" : `${progress}%` }}
              />
            </div>
          </div>
        )}

        {errorMsg && (
          <p className="mt-3 text-sm text-danger-700">{errorMsg}</p>
        )}

        {/* 操作按钮：只在出错时显示，正常流程全自动 */}
        <div className="mt-5 flex flex-col gap-2">
          {!hasDesktop && (
            <Button
              variant="primary"
              onClick={() => { onClose(); onViewSettings(); }}
              className="w-full"
            >
              查看更新
            </Button>
          )}
          {phase === "error" && (
            <Button
              variant="primary"
              onClick={startDownload}
              className="w-full"
            >
              重试下载
            </Button>
          )}
          <Button
            variant="ghost"
            onClick={onClose}
            className="w-full"
          >
            稍后再说
          </Button>
        </div>
      </div>
    </div>
  );
}
