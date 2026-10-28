// features/automation —— 商品必要字段诊断（老版 diagnostics-heading 区）。
//
// 老版在开始处理前就逐项列出「标题 / 描述 / 图片 / 类目 / 重量 / 包裹尺寸 /
// SKU 价格 / 含税价 / 库存 / SKU 重量」的检查状态，不合格直接不让保存
// （product_workflow.diagnose_item）。后端 /collect-box/{id} 一直在返回
// diagnostics + readyToProcess，但界面上从没展示过，等于这条链路断在客户端。
//
// 只在勾选了「恰好一件」时查：诊断要回源拉详情，逐件查会打爆上游。
// 多选时不显示这块，由后端在逐件入仓时各自把关。

import { AlertTriangle, CheckCircle2, Stethoscope } from "lucide-react";
import { Badge, Card, CardTitle, SkeletonRows } from "@/components/ui";
import { useCollectBoxDetail } from "@/lib/queries";
import type { Diagnostic } from "@/lib/types";

export interface DiagnosePanelProps {
  /** 勾选集合；只有恰好一件时才诊断 */
  selected: { detailId: string; shopId: string }[];
}

/** 诊断码 -> 人话字段名（老版 fieldNames 的同款映射） */
const FIELD_NAMES: Record<string, string> = {
  missing_title: "商品标题",
  title_length: "标题长度",
  missing_notes: "商品描述",
  missing_images: "商品图片",
  missing_category: "商品类目",
  missing_weight: "商品重量",
  missing_packageLength: "包裹长度",
  missing_packageWidth: "包裹宽度",
  missing_packageHeight: "包裹高度",
  missing_sku: "SKU",
  invalid_sku_price: "SKU 原价格",
  invalid_sku_price_include_vat: "SKU 含税价",
  invalid_sku_stock: "SKU 库存",
  missing_sku_weight: "SKU 重量",
};

function labelOf(entry: Diagnostic): string {
  return FIELD_NAMES[entry.code] || entry.code;
}

export function DiagnosePanel({ selected }: DiagnosePanelProps) {
  const single = selected.length === 1 ? selected[0] : null;
  const query = useCollectBoxDetail(single?.detailId ?? null, single?.shopId ?? null);

  // 没勾选或勾了多件：不占版面
  if (!single) return null;

  const data = query.data;
  const issues = data?.diagnostics ?? [];
  // 标题长度只是提醒，不阻止保存
  const errors = issues.filter((item) => item.severity !== "warning");
  const warnings = issues.filter((item) => item.severity === "warning");

  return (
    <Card padding="normal">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5">
          <Stethoscope className="size-4 text-fg-muted" strokeWidth={1.75} />
          <CardTitle>字段诊断</CardTitle>
        </span>
        {query.isLoading ? null : data ? (
          errors.length > 0 ? (
            <Badge variant="warning">{errors.length} 项不合格</Badge>
          ) : warnings.length > 0 ? (
            <Badge variant="neutral">{warnings.length} 项提醒</Badge>
          ) : (
            <Badge variant="success">检查通过</Badge>
          )
        ) : null}
      </div>

      {query.isLoading ? (
        <div className="mt-2">
          <SkeletonRows rows={3} />
        </div>
      ) : query.isError ? (
        <p className="mt-2 text-2xs leading-4 text-warning-700">
          诊断失败：回源拉详情出错，稍后重试。
        </p>
      ) : !data ? null : issues.length === 0 ? (
        <p className="mt-2 flex items-center gap-1.5 text-2xs leading-4 text-success-700">
          <CheckCircle2 className="size-3.5 shrink-0" strokeWidth={2} />
          必要字段齐全，可以处理。
        </p>
      ) : (
        <ul className="mt-2 flex flex-col gap-1">
          {issues.map((entry, index) => {
            const isWarning = entry.severity === "warning";
            return (
              <li
                key={`${entry.code}-${index}`}
                className={
                  isWarning
                    ? "flex items-start gap-1.5 rounded border border-line bg-surface-sunken px-2 py-1"
                    : "flex items-start gap-1.5 rounded border border-warning-200 bg-warning-50 px-2 py-1.5"
                }
              >
                <AlertTriangle
                  className={
                    isWarning
                      ? "mt-0.5 size-3 shrink-0 text-fg-subtle"
                      : "mt-0.5 size-3 shrink-0 text-warning-700"
                  }
                  strokeWidth={2}
                />
                <span className="min-w-0">
                  <span className="text-2xs font-medium text-fg">{labelOf(entry)}</span>
                  <span
                    className={
                      isWarning
                        ? "ml-1.5 text-2xs leading-4 text-fg-muted"
                        : "ml-1.5 text-2xs leading-4 text-warning-700"
                    }
                  >
                    {entry.message}
                  </span>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

export default DiagnosePanel;
