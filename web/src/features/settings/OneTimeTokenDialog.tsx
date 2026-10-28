// features/settings —— 一次性 token 展示框
//
// 后端只存 token 的哈希，明文只在「新建操作员」和「重置 token」这两个响应里出现一次。
// 关掉弹窗之后谁都拿不回来（连管理员也不行），所以这个框的职责只有一个：
// 让人当场把它复制走。文案写死，不改。

import { useState } from "react";
import { Check, Copy, KeyRound } from "lucide-react";
import { Badge, Button, Dialog, Field, Input, toast } from "@/components/ui";

/** 写死的警示语（需求要求逐字一致，改动前先确认产品口径） */
export const ONE_TIME_TOKEN_NOTICE = "这串 token 只显示这一次，关掉就看不到了。请现在复制给对方";

export interface OneTimeTokenDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** token 明文；null 时不渲染内容（弹窗已关闭） */
  token: string | null;
  /** 这个 token 属于谁（标题里带上名字，避免复制错了发给别人） */
  operatorName?: string;
  /** 顶部说明，默认针对「新建」；重置时传「旧 token 已立刻失效」那一句 */
  description?: string;
  /** 关闭回调（复制与否都允许关，但按钮上要提示风险） */
  onDone?: () => void;
}

export function OneTimeTokenDialog({
  open,
  onOpenChange,
  token,
  operatorName,
  description,
  onDone,
}: OneTimeTokenDialogProps) {
  const [copied, setCopied] = useState(false);

  const copy = (): void => {
    if (!token) return;
    if (!navigator.clipboard) {
      toast.error("当前环境不支持剪贴板，请手动选中 token 复制");
      return;
    }
    navigator.clipboard.writeText(token).then(
      () => {
        setCopied(true);
        toast.success("已复制 token", { description: "发给对方，让他填进「操作员身份」里" });
      },
      () => toast.error("复制失败，请手动选中 token 复制")
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setCopied(false);
          onOpenChange(false);
          onDone?.();
        }
      }}
      size="md"
      title={operatorName ? `「${operatorName}」的 token` : "操作员 token"}
      description={description}
      // 关掉就再也看不到，误触遮罩代价太大
      closeOnOverlay={false}
      footer={
        <>
          <Button
            variant="ghost"
            onClick={() => {
              setCopied(false);
              onOpenChange(false);
              onDone?.();
            }}
          >
            我已复制，关闭
          </Button>
          <Button
            variant="primary"
            icon={copied ? <Check /> : <Copy />}
            onClick={copy}
            disabled={!token}
          >
            {copied ? "已复制" : "复制"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3.5">
        {/* 警示语用整块底色压住，避免被当成普通说明跳过 */}
        <div className="rounded-md border border-warning-600/25 bg-warning-50 px-3 py-2.5 text-[13px] font-medium leading-5 text-warning-700">
          {ONE_TIME_TOKEN_NOTICE}
        </div>

        <Field
          label="token"
          hint="对方在「设置 → 操作员」里粘贴这一串，就能以这个身份操作，改动会记在他名下。"
        >
          <div className="flex items-center gap-2">
            <Input
              readOnly
              value={token ?? ""}
              onFocus={(event) => event.currentTarget.select()}
              className="font-mono text-xs"
              aria-label="一次性操作员 token"
            />
            <Button variant="secondary" icon={<Copy />} onClick={copy} disabled={!token}>
              复制
            </Button>
          </div>
        </Field>

        <div className="flex items-center gap-1.5 text-2xs leading-4 text-fg-muted">
          <KeyRound aria-hidden className="size-3" strokeWidth={1.75} />
          服务端只保存这串 token 的哈希，任何人都无法再次查看明文。
          <Badge variant="neutral" size="sm">
            仅此一次
          </Badge>
        </div>
      </div>
    </Dialog>
  );
}

export default OneTimeTokenDialog;
