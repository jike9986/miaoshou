import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { HardDrive } from "lucide-react";
import { Button, Card, CardDescription, CardTitle, toast } from "@/components/ui";
import { apiErrorMessage } from "@/lib/api";
import { clearOperatorProductListCache } from "@/lib/operatorQueryClientRegistry.mjs";

export function LocalCachePanel() {
  const client = useQueryClient();
  const [clearing, setClearing] = useState(false);

  const clearCache = async (): Promise<void> => {
    setClearing(true);
    try {
      await clearOperatorProductListCache(client);
      toast.success("当前账号的商品缓存已清除", {
        description: "下次打开采集箱或成品仓库时会重新加载。",
      });
    } catch (error) {
      toast.error("缓存清除失败", { description: apiErrorMessage(error) });
    } finally {
      setClearing(false);
    }
  };

  return (
    <Card padding="normal">
      <div className="flex items-center gap-2">
        <HardDrive className="size-4 text-fg-muted" strokeWidth={1.75} />
        <CardTitle>本机缓存</CardTitle>
      </div>
      <CardDescription>
        商品列表缓存在这台电脑上，方便快速打开；进入页面后会自动核对最新数据。
      </CardDescription>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
        <p className="min-w-0 flex-1 text-sm text-fg-secondary">
          清除当前账号的采集箱和成品仓库缓存，保留登录、妙手绑定和服务器商品。
        </p>
        <Button
          variant="secondary"
          loading={clearing}
          loadingText="清除中"
          onClick={() => void clearCache()}
        >
          清除商品缓存
        </Button>
      </div>
    </Card>
  );
}
