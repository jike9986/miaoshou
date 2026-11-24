// features/warehouse —— 定价模板弹窗
//
// 实体在 `PriceTemplateEditor`（设置页内联也要用同一份），这里只负责
// 外面那层 Dialog。参数列表和保存逻辑都不许写回这里 ——
// 写两遍必然漂移，之前就是这样：一处加了字段另一处改不了，
// 用户看到的价永远对不上。

import { Button, Dialog } from "@/components/ui";
import { PriceTemplateEditor } from "@/features/warehouse/PriceTemplateEditor";

export interface PriceTemplateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function PriceTemplateDialog({ open, onOpenChange }: PriceTemplateDialogProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="2xl"
      closeOnOverlay={false}
      title="定价模板"
      description="模板参数决定售价怎么算。右边可随时试算，改完不必先保存。"
      footer={
        <Button variant="ghost" onClick={() => onOpenChange(false)}>
          关闭
        </Button>
      }
    >
      <PriceTemplateEditor
        active={open}
        showActions
        onSaved={() => onOpenChange(false)}
      />
    </Dialog>
  );
}