// features/settings —— 发布设置（全局发布总开关）
//
// 这个开关控制的是「仓库页和自动化页的发布/认领功能是否可用」。
// 后端 publishEnabled=false 时，所有写操作会被拒（403 PUBLISH_DISABLED）。
// 放在设置页而不是仓库页：它是全局配置，不是某次发布的参数。

import { Globe } from "lucide-react";
import { useAuthStatus } from "@/lib/queries";
import { api, apiErrorMessage } from "@/lib/api";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardDescription, CardTitle, Switch, toast } from "@/components/ui";

export function PublishSettingsPanel() {
  const { data: status } = useAuthStatus();
  const queryClient = useQueryClient();

  const toggle = useMutation({
    mutationFn: (enabled: boolean) =>
      api.post("/system/settings", { publishEnabled: enabled }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.auth.all });
      void queryClient.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (error) => toast.error("切换失败", { description: apiErrorMessage(error) }),
  });

  const enabled = status?.publishEnabled ?? false;

  return (
    <Card padding="normal">
      <div className="flex items-center gap-2">
        <Globe className="size-4 text-fg-muted" strokeWidth={1.75} />
        <CardTitle>发布总开关</CardTitle>
      </div>
      <CardDescription>
        关闭后，仓库页和自动化页的「发布到店铺」「认领到采集箱」按钮会被禁用。
        已有批次和定时任务不受影响。
      </CardDescription>

      <div className="mt-4 flex items-center justify-between rounded-md border border-border px-4 py-3">
        <div>
          <p className="text-[13px] font-medium text-fg">
            {enabled ? "发布功能已开启" : "发布功能已关闭"}
          </p>
          <p className="mt-0.5 text-xs text-fg-muted">
            {enabled
              ? "仓库页和自动化页可以发布 / 认领到店铺"
              : "所有发布 / 认领操作都会被后端拒绝"}
          </p>
        </div>
        <Switch
          checked={enabled}
          onChange={(checked) => toggle.mutate(checked)}
          disabled={toggle.isPending}
          aria-label="发布总开关"
        />
      </div>

      {enabled && (
        <p className="mt-2 text-2xs text-fg-muted">
          开启后，自动化页处理成功的商品可以认领到所选店铺的采集箱（仍需人工确认后上架）。
        </p>
      )}
    </Card>
  );
}
