import { RefreshCw, ShieldCheck } from "lucide-react";
import { Button, Card, CardDescription, CardTitle, toast } from "@/components/ui";
import { UpdateActions } from "@/components/layout/Sidebar";
import { apiErrorMessage } from "@/lib/api";
import { useCheckUpdate } from "@/lib/queries";

export function VersionPanel() {
  const check = useCheckUpdate();
  const result = check.data;
  const currentVersion = window.__DSH_BOOT__?.version ?? result?.current ?? "未知";

  const checkNow = (): void => {
    check.mutate(undefined, {
      onSuccess: (data) => {
        if (data.updateAvailable) toast.info(`发现新版本 ${data.latest}`);
        else if (data.ok) toast.success("已是最新版本");
        else toast.error(data.error ?? "检查更新失败");
      },
      onError: (error) => toast.error(apiErrorMessage(error)),
    });
  };

  return (
    <Card padding="normal">
      <div className="flex items-center gap-2">
        <ShieldCheck className="size-4 text-fg-muted" strokeWidth={1.75} />
        <CardTitle>关于与更新</CardTitle>
      </div>
      <CardDescription>当前版本与更新检查</CardDescription>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
        <div>
          <p className="text-[13px] font-medium text-fg">奥皇上品</p>
          <p className="mt-0.5 text-xs text-fg-muted">当前版本 v{currentVersion}</p>
        </div>
        <Button
          variant="secondary"
          icon={<RefreshCw />}
          loading={check.isPending}
          onClick={checkNow}
        >
          检查更新
        </Button>
      </div>

      {result?.ok && result.updateAvailable && (
        <div className="mt-4 border-t border-border pt-4">
          <p className="text-[13px] font-medium text-brand-700">发现新版本 v{result.latest}</p>
          {result.notes && (
            <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-fg-secondary">
              {result.notes}
            </p>
          )}
          <div className="mt-3">
            <UpdateActions version={result} />
          </div>
        </div>
      )}

      {result && !result.ok && (
        <p role="status" className="mt-3 text-xs text-danger-700">
          {result.error ?? "更新信息暂不可用"}
        </p>
      )}
    </Card>
  );
}
