// 自动更新弹窗：检测到新版本时自动弹出，支持软件内下载 + 重启安装。
// 只在 Electron 桌面端（window.desktop?.update 存在）走软件内更新；
// 浏览器环境退回「查看更新」跳到设置页。

import { useEffect, useRef, useState } from "react";
import { Download, Play, RefreshCw } from "lucide-react";
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

  // 检查是否已有缓存的安装包
  useEffect(() => {
    if (!desktopUpdate) return;
    void desktopUpdate.pending().then((p: { path: string; version: string } | null) => {
      if (p && p.version === version.latest) setPhase("done");
    });
  }, [desktopUpdate, version.latest]);

  // 订阅进度
  useEffect(() => {
    if (!desktopUpdate) return;
    return desktopUpdate.onProgress((p: { received: number; total: number; percent: number }) => {
      setProgress(p.percent);
    });
  }, [desktopUpdate]);

  const handleDownload = (): void => {
    if (!desktopUpdate) return;
    const manifest = {
      version: String(version.latest || ""),
      url: String(version.url || ""),
      sha256: String(version.sha256 || ""),
      sizeBytes: version.sizeBytes ?? undefined,
      notes: version.notes ?? undefined,
    };
    if (!manifest.url || !manifest.sha256) {
      setErrorMsg("更新清单缺少下载地址或校验值，请稍后再试");
      return;
    }
    setPhase("downloading");
    setProgress(0);
    setErrorMsg(null);
    void desktopUpdate.download(manifest).then((result: { ok: boolean; error?: string; size?: number }) => {
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
        setErrorMsg(result.error || "启动安装程序失败");
        toast.error("启动安装程序失败", { description: result.error });
        return;
      }
      toast.success("正在退出并安装", { description: "安装完成后请重新打开软件" });
    });
  };

  return (
    // 半透明遮罩
    <div
      className="fixed inset-0 z-[200] flex items-center justify-center bg-black/40"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="mx-4 w-full max-w-sm rounded-xl border border-border bg-surface p-6 shadow-2xl">
        {/* 标题 */}
        <div className="flex items-start gap-3">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-brand-100 text-brand-600">
            <RefreshCw className="size-5" strokeWidth={2} />
          </div>
          <div className="min-w-0">
            <p className="font-semibold text-fg">发现新版本</p>
            <p className="text-sm text-fg-muted">
              当前 {version.current} → 新版 <span className="font-medium text-fg">{version.latest}</span>
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
        {phase === "downloading" && (
          <div className="mt-4">
            <div className="flex justify-between text-xs text-fg-muted">
              <span>正在下载…</span>
              <span>{progress}%</span>
            </div>
            <div className="mt-1 h-2 overflow-hidden rounded-full bg-bg-subtle">
              <div
                className="h-full rounded-full bg-brand-600 transition-all duration-200"
                style={{ width: `${progress}%` }}
              />
            </div>
          </div>
        )}

        {phase === "done" && (
          <p className="mt-4 text-sm text-success-700">✓ 安装包已下载并校验通过，可立即安装</p>
        )}

        {errorMsg && (
          <p className="mt-3 text-sm text-danger-700">{errorMsg}</p>
        )}

        {/* 操作按钮 */}
        <div className="mt-5 flex flex-col gap-2">
          {hasDesktop ? (
            <>
              {phase === "done" ? (
                <Button
                  variant="primary"
                  icon={<Play />}
                  onClick={handleInstall}
                  className="w-full"
                >
                  重启并安装
                </Button>
              ) : (
                <Button
                  variant="primary"
                  icon={<Download />}
                  disabled={phase === "downloading"}
                  loading={phase === "downloading"}
                  onClick={handleDownload}
                  className="w-full"
                >
                  {phase === "downloading" ? `下载中 ${progress}%` : "立即更新"}
                </Button>
              )}
            </>
          ) : (
            <Button
              variant="primary"
              icon={<Download />}
              onClick={() => { onClose(); onViewSettings(); }}
              className="w-full"
            >
              查看更新
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
