// features/warehouse —— 价格试算弹窗（等价老版「价格预览」）
//
// 后端 POST /warehouse/price-preview 只算价：不发布、不写库、不调上游。
// 老版这块是一个常驻的「价格预览」面板；新版仓库有选中态，用弹窗更省空间。
//
// 交互：选模板（可留空走默认）→ 可选填汇率 → 试算 → 看「原价 → 现价」。

import { useEffect, useState } from "react";
import { Calculator, RefreshCw } from "lucide-react";
import { ApiError, apiErrorMessage } from "@/lib/api";
import { usePricePreview, useTemplates } from "@/lib/queries";
import type { PricePreviewEntry } from "@/lib/queries";
import {
  Badge,
  Button,
  Dialog,
  Field,
  InlineError,
  Select,
  Separator,
  toast,
} from "@/components/ui";

export interface PricePreviewDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 选中的仓库商品 id 列表 */
  ids: string[];
}

function money(min: string | null, max: string | null, currency: string): string {
  const fmt = (value: string | null): string => (value === null || value === "" ? "—" : value);
  if (min === null && max === null) return "—";
  if (min === max) return `${fmt(min)} ${currency}`;
  return `${fmt(min)}–${fmt(max)} ${currency}`;
}

export function PricePreviewDialog({ open, onOpenChange, ids }: PricePreviewDialogProps) {
  const templatesQuery = useTemplates(open);
  const templates = templatesQuery.data?.templates ?? [];
  // 服务端 selected 可能为空（模板只是拉下来、还没选过）。
  // 这时兜底用第一个可用模板，否则用户点「试算」会直接吃 422 请先选择定价模板。
  const usableTemplates = templates.filter((item) => !item.invalid);
  const selectedTemplate =
    templatesQuery.data?.selected?.[0] ?? usableTemplates[0]?.name ?? "";

  const [template, setTemplate] = useState("");
  const [exchangeRate, setExchangeRate] = useState("");
  const [error, setError] = useState<string | null>(null);
  const preview = usePricePreview();

  // 模板加载完成后兜底：
  //   服务端 selected 为空时用第一个可用模板，否则点了「试算」直接吃 422。
  // 依赖 templates.length，确保数据到位后才会生效（不是只在 open 那一刻跑）。
  useEffect(() => {
    if (!open) return;
    if (template) return;
    if (!templates.length) return;
    if (selectedTemplate) setTemplate(selectedTemplate);
  }, [open, template, selectedTemplate, templates.length]);

  // 关闭时清掉上一次结果，避免下次打开看到旧数据
  useEffect(() => {
    if (!open) {
      preview.reset();
      setError(null);
    }
    // preview.reset 是稳定引用，不放进依赖以免每次渲染都触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const run = (): void => {
    if (ids.length === 0) {
      setError("请先在列表里勾选商品");
      return;
    }
    setError(null);
    preview.mutate(
      {
        items: ids.map((id) => ({ warehouseId: id })),
        template: template || null,
        exchangeRate: exchangeRate.trim() || null,
      },
      {
        onError: (err) => setError(apiErrorMessage(err)),
        onSuccess: (data) => {
          if (data.repriced === 0) {
            const first = data.warnings[0];
            setError(first?.message ?? "没有可以试算的商品");
            return;
          }
          toast.success(`已试算 ${data.repriced} 件`);
        },
      }
    );
  };

  const data = preview.data;
  const entries: PricePreviewEntry[] = data?.items ?? [];

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title="价格试算"
      description="按定价模板算出售价，只算不改。不写库、不发布、不调上游。"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
          <Button
            variant="primary"
            icon={preview.isPending ? <RefreshCw className="size-4 animate-spin" /> : <Calculator />}
            loading={preview.isPending}
            disabled={ids.length === 0}
            onClick={run}
          >
            试算{ids.length > 0 ? ` ${ids.length} 件` : ""}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="定价模板" hint="留空则用当前选中的模板">
            <Select
              value={template}
              onChange={(value) => setTemplate(value)}
              disabled={preview.isPending || templatesQuery.isLoading}
              options={[
                { value: "", label: "使用默认模板" },
                ...templates.map((item) => ({
                  value: item.name,
                  label: item.invalid ? `${item.name}（不可用）` : item.name,
                })),
              ]}
            />
          </Field>
          <Field label="汇率" hint="留空则用商品记录里的汇率">
            <input
              type="text"
              inputMode="decimal"
              value={exchangeRate}
              onChange={(event) => setExchangeRate(event.target.value)}
              placeholder="如 1.55"
              disabled={preview.isPending}
              className="h-9 w-full rounded-md border border-border bg-canvas px-3 text-sm text-fg placeholder:text-fg-muted focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/20 disabled:opacity-50"
            />
          </Field>
        </div>

        {templatesQuery.isError && (
          <InlineError>{apiErrorMessage(templatesQuery.error as ApiError)}</InlineError>
        )}
        {error && <InlineError>{error}</InlineError>}

        {data && (
          <>
            <Separator />
            <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
              <Badge variant="neutral">模板：{data.template.name ?? "默认"}</Badge>
              <Badge variant="neutral">币种：{data.template.sellingCurrency}</Badge>
              {data.exchangeRate !== null && (
                <Badge variant="neutral">汇率：{String(data.exchangeRate)}</Badge>
              )}
              <span className="num">
                共 {data.total} 件，试算 {data.repriced} 件
                {data.skipped > 0 ? `，跳过 ${data.skipped} 件` : ""}
              </span>
            </div>

            <div className="max-h-[340px] overflow-auto overscroll-contain rounded-md border border-border">
              <table className="w-full border-collapse text-xs">
                <thead className="sticky top-0 z-10 bg-canvas-inset">
                  <tr className="text-left text-fg-muted">
                    <th className="px-2.5 py-2 font-medium">商品</th>
                    <th className="px-2.5 py-2 font-medium">采购价</th>
                    <th className="px-2.5 py-2 font-medium">原售价</th>
                    <th className="px-2.5 py-2 font-medium">试算售价</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((entry) => (
                    <tr key={entry.warehouseId} className="border-t border-border align-top">
                      <td className="max-w-[240px] px-2.5 py-2">
                        <span className="line-clamp-2 text-fg">{entry.title || "（无标题）"}</span>
                      </td>
                      <td className="num whitespace-nowrap px-2.5 py-2 text-fg-secondary">
                        {money(entry.purchasePrice.min, entry.purchasePrice.max, "CNY")}
                      </td>
                      <td className="num whitespace-nowrap px-2.5 py-2 text-fg-muted">
                        {money(entry.priceBefore.min, entry.priceBefore.max, entry.currency)}
                      </td>
                      <td className="num whitespace-nowrap px-2.5 py-2 font-medium text-brand-700">
                        {money(entry.priceAfter.min, entry.priceAfter.max, entry.currency)}
                        {entry.calculation?.isEstimate && (
                          <span
                            className="ml-1 rounded bg-warning/10 px-1 text-2xs font-normal text-warning-700"
                            title={`模板里没填这些费率，已按 0 计算：${
                              (entry.calculation?.missingRateFields || []).join("、")
                            }`}
                          >
                            估算
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {data.warnings.length > 0 && (
              <div className="rounded-md border border-border bg-canvas-inset/50 p-2.5 text-xs text-fg-muted">
                <div className="mb-1 font-medium text-fg-secondary">跳过的商品</div>
                <ul className="flex flex-col gap-0.5">
                  {data.warnings.slice(0, 8).map((warning) => (
                    <li key={`${warning.index}-${warning.code}`}>
                      {warning.title ? `${warning.title}：` : ""}
                      {warning.message}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}

        {!data && !preview.isPending && (
          <div className="flex h-[120px] items-center justify-center text-xs text-fg-muted">
            选择模板后点「试算」，这里会列出原价和试算售价。
          </div>
        )}
      </div>
    </Dialog>
  );
}
