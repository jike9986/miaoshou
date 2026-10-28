// features/settings —— 审计日志的展示字典（动作 / 结果）
//
// 单独成文件的原因：这些是「文案映射」而不是组件，混在 AuditLogPanel.tsx 里
// 会让那个文件既导出组件又导出常量，热更新（Fast Refresh）就退化成整页刷新。
//
// 后端给的是英文点号标识（warehouse.patch / publish.submit …），直接摊给用户看不懂；
// 没收录的新动作**原样显示标识**，不做「未知动作」占位 —— 原样显示至少还能拿去搜后端代码。

import type { BadgeVariant } from "@/components/ui";

export const AUDIT_ACTION_TEXT: Record<string, string> = {
  "operator.create": "新建操作员",
  "operator.rename": "修改操作员名字",
  "operator.disable": "停用操作员",
  "operator.enable": "启用操作员",
  "operator.rotate": "重置 token",
  "warehouse.patch": "修改商品",
  "warehouse.delete": "删除商品",
  "warehouse.claim": "认领商品",
  "warehouse.release": "释放认领",
  "warehouse.import": "导入商品",
  "warehouse.transform": "批量改价 / 删图",
  "warehouse.batch_category": "批量设置分类",
  "warehouse.batch_group": "批量设置分组",
  "image.translate": "翻译图片",
  "image.upload": "上传图片",
  "image.reconcile": "核对图片",
  "publish.submit": "提交发布",
  "publish.retry": "重试发布",
  "publish.cancel": "取消发布",
  "batch.create": "创建批次",
  "batch.control": "控制批次",
  "batch.retry": "重试批次",
  "batch.delete": "删除批次",
  "schedule.create": "新建定时任务",
  "schedule.patch": "修改定时任务",
  "schedule.delete": "删除定时任务",
  "schedule.run": "手动执行定时任务",
  "words.create": "新增违规词",
  "words.delete": "删除违规词",
  "auth.connect": "连接妙手",
  "system.update": "检查更新",
};

/** 动作标识 → 中文；没收录就原样返回标识 */
export function auditActionText(action: string): string {
  return AUDIT_ACTION_TEXT[action] ?? action;
}

/** 动作筛选下拉的候选项（第一项是「全部」） */
export const AUDIT_ACTION_OPTIONS = [
  { value: "", label: "全部动作" },
  ...Object.entries(AUDIT_ACTION_TEXT).map(([value, label]) => ({ value, label })),
];

const AUDIT_RESULT_META: Record<string, { label: string; variant: BadgeVariant }> = {
  success: { label: "成功", variant: "success" },
  failed: { label: "失败", variant: "danger" },
  blocked: { label: "已拦截", variant: "warning" },
};

/** 结果 → 徽标（失败 danger / 拦截 warning，其余中性） */
export function auditResultMeta(result: string): { label: string; variant: BadgeVariant } {
  return AUDIT_RESULT_META[result] ?? { label: result || "—", variant: "neutral" };
}
