// features/warehouse —— 定价模板编辑器（等价老版「定价模板」面板）
//
// 老版 index.html 里的 #price-template-panel，字段顺序固定：
//   本地已存模板 / 模板参数 / 一般无需修改（折叠）/ 售价试算
//
// 后端 POST /system/templates 早已存在并做完整校验（services/price_template.py），
// 20 个字段全部支持，但前端此前没有任何编辑器 —— 界面上根本改不了模板。
//
// 这里的字段名与后端 DEFAULT_TEMPLATE 一一对应，不做重命名，避免两边漂移。

import { useEffect, useMemo, useState } from "react";
import { ChevronDown, RefreshCw, Save, Trash2 } from "lucide-react";
import { apiErrorMessage } from "@/lib/api";
import {
  useDeleteTemplate,
  useSaveTemplate,
  useSyncPriceTemplates,
  useTemplates,
} from "@/lib/queries";
import {
  Button,
  Dialog,
  Field,
  InlineError,
  Input,
  NumberInput,
  Select,
  Separator,
  toast,
} from "@/components/ui";

/** 与后端 DEFAULT_TEMPLATE 同名的可编辑字段。 */
export interface TemplateForm {
  name: string;
  site: string;
  currency: string;
  profitType: string;
  profitPercent: string;
  lastMileFreightCny: string;
  platformCommissionPercent: string;
  paymentChargePercent: string;
  vatChargePercent: string;
  withdrawChargePercent: string;
  bcpChargePercent: string;
  sfpChargePercent: string;
  affiliateChargePercent: string;
  platformSupportCny: string;
  lastMileFreightServiceTaxPercent: string;
  otherChargeCny: string;
  firstWeightGrams: string;
  firstWeightPrice: string;
  continuedUnitGrams: string;
  continuedUnitPrice: string;
}

const EMPTY_FORM: TemplateForm = {
  name: "",
  site: "MY",
  currency: "CNY",
  profitType: "bySalePriceProfit",
  profitPercent: "20",
  lastMileFreightCny: "16.63",
  platformCommissionPercent: "",
  paymentChargePercent: "3.78",
  vatChargePercent: "10",
  withdrawChargePercent: "1",
  bcpChargePercent: "3.24",
  sfpChargePercent: "",
  affiliateChargePercent: "",
  platformSupportCny: "0.83",
  lastMileFreightServiceTaxPercent: "",
  otherChargeCny: "",
  firstWeightGrams: "30",
  firstWeightPrice: "0.18",
  continuedUnitGrams: "10",
  continuedUnitPrice: "0.12",
};

/** 空串 -> null（后端把 null 当「不参与计算」），有值则原样交给后端做数字校验。 */
function toNumberOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function buildTemplate(form: TemplateForm): Record<string, unknown> {
  return {
    name: form.name.trim(),
    site: form.site.trim().toUpperCase(),
    currency: form.currency.trim().toUpperCase(),
    profitType: form.profitType,
    profitPercent: toNumberOrNull(form.profitPercent),
    lastMileFreightCny: toNumberOrNull(form.lastMileFreightCny),
    platformCommissionPercent: toNumberOrNull(form.platformCommissionPercent),
    paymentChargePercent: toNumberOrNull(form.paymentChargePercent),
    vatChargePercent: toNumberOrNull(form.vatChargePercent),
    withdrawChargePercent: toNumberOrNull(form.withdrawChargePercent),
    bcpChargePercent: toNumberOrNull(form.bcpChargePercent),
    sfpChargePercent: toNumberOrNull(form.sfpChargePercent),
    affiliateChargePercent: toNumberOrNull(form.affiliateChargePercent),
    platformSupportCny: toNumberOrNull(form.platformSupportCny),
    lastMileFreightServiceTaxPercent: toNumberOrNull(form.lastMileFreightServiceTaxPercent),
    otherChargeCny: toNumberOrNull(form.otherChargeCny),
    // 首重/续重是嵌套的 freightTable，后端 normalize 会校验
    freightTable: {
      firstWeightGrams: toNumberOrNull(form.firstWeightGrams),
      firstWeightPrice: toNumberOrNull(form.firstWeightPrice),
      continuedUnitGrams: toNumberOrNull(form.continuedUnitGrams),
      continuedUnitPrice: toNumberOrNull(form.continuedUnitPrice),
    },
  };
}

/** 把后端模板对象摊平回表单（缺的字段留空，不臆造默认值）。 */
function formFromTemplate(raw: Record<string, unknown> | undefined, fallbackName: string): TemplateForm {
  if (!raw) return { ...EMPTY_FORM, name: fallbackName };
  const table = (raw.freightTable ?? {}) as Record<string, unknown>;
  const text = (value: unknown): string =>
    value === null || value === undefined || value === "" ? "" : String(value);
  return {
    name: text(raw.name) || fallbackName,
    site: text(raw.site) || "MY",
    currency: text(raw.currency) || "CNY",
    profitType: text(raw.profitType) || "bySalePriceProfit",
    profitPercent: text(raw.profitPercent),
    lastMileFreightCny: text(raw.lastMileFreightCny),
    platformCommissionPercent: text(raw.platformCommissionPercent),
    paymentChargePercent: text(raw.paymentChargePercent),
    vatChargePercent: text(raw.vatChargePercent),
    withdrawChargePercent: text(raw.withdrawChargePercent),
    bcpChargePercent: text(raw.bcpChargePercent),
    sfpChargePercent: text(raw.sfpChargePercent),
    affiliateChargePercent: text(raw.affiliateChargePercent),
    platformSupportCny: text(raw.platformSupportCny),
    lastMileFreightServiceTaxPercent: text(raw.lastMileFreightServiceTaxPercent),
    otherChargeCny: text(raw.otherChargeCny),
    firstWeightGrams: text(table.firstWeightGrams),
    firstWeightPrice: text(table.firstWeightPrice),
    continuedUnitGrams: text(table.continuedUnitGrams),
    continuedUnitPrice: text(table.continuedUnitPrice),
  };
}

export interface PriceTemplateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function PriceTemplateDialog({ open, onOpenChange }: PriceTemplateDialogProps) {
  const templatesQuery = useTemplates(open);
  const saveTemplate = useSaveTemplate();
  const deleteTemplate = useDeleteTemplate();
  const syncTemplates = useSyncPriceTemplates();

  const [form, setForm] = useState<TemplateForm>(EMPTY_FORM);
  const [picked, setPicked] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const templates = templatesQuery.data?.templates ?? [];
  const templateOptions = useMemo(
    () => [
      { value: "", label: "新建模板" },
      ...templates.map((item) => ({
        value: item.name,
        label: item.invalid ? `${item.name}（不可用）` : item.name,
      })),
    ],
    [templates]
  );

  // 打开时默认选中第一个模板，把它填进表单
  useEffect(() => {
    if (!open) return;
    if (picked) return;
    const first = templates.find((item) => !item.invalid) ?? templates[0];
    if (first) setPicked(first.name);
  }, [open, picked, templates]);

  // 选中项变化 -> 回填表单
  useEffect(() => {
    if (!open || !picked) return;
    const found = templates.find((item) => item.name === picked);
    if (found?.template) {
      setForm(formFromTemplate(found.template as Record<string, unknown>, found.name));
    }
  }, [open, picked, templates]);

  const set = <K extends keyof TemplateForm>(key: K, value: TemplateForm[K]): void => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const save = (): void => {
    const name = form.name.trim();
    if (!name) {
      setError("请填写模板名称");
      return;
    }
    setError(null);
    saveTemplate.mutate(buildTemplate(form), {
      onSuccess: () => {
        toast.success(`已保存模板「${name}」`);
        setPicked(name);
      },
      onError: (err) => setError(apiErrorMessage(err)),
    });
  };

  const remove = (): void => {
    if (!picked) return;
    deleteTemplate.mutate(picked, {
      onSuccess: () => {
        toast.success(`已删除模板「${picked}」`);
        setPicked("");
        setForm(EMPTY_FORM);
      },
      onError: (err) => setError(apiErrorMessage(err)),
    });
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title="定价模板"
      description="模板参数决定售价怎么算。保存后，价格试算和自动上品都按它走。"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
          <Button
            variant="primary"
            icon={<Save />}
            loading={saveTemplate.isPending}
            onClick={save}
          >
            保存模板
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {/* ---------- 本地已存模板 ---------- */}
        <div className="flex flex-col gap-2">
          <div className="text-xs font-medium text-fg-secondary">本地已存模板</div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="min-w-[180px] flex-1">
              <Select
                value={picked}
                onChange={(value) => {
                  setPicked(value);
                  if (!value) setForm(EMPTY_FORM);
                }}
                options={templateOptions}
                disabled={templatesQuery.isLoading}
              />
            </div>
            <Button
              size="sm"
              variant="secondary"
              icon={<RefreshCw className={syncTemplates.isPending ? "animate-spin" : undefined} />}
              loading={syncTemplates.isPending}
              onClick={() =>
                syncTemplates.mutate({}, {
                  onSuccess: () => toast.success("已从妙手拉取模板列表"),
                  onError: (err) => setError(apiErrorMessage(err)),
                })
              }
            >
              从妙手同步
            </Button>
            <Button
              size="sm"
              variant="ghost"
              icon={<Trash2 />}
              disabled={!picked}
              loading={deleteTemplate.isPending}
              onClick={remove}
            >
              删除
            </Button>
          </div>
        </div>

        <Separator />

        {/* ---------- 模板参数 ---------- */}
        <div className="flex flex-col gap-3">
          <div className="text-xs font-medium text-fg-secondary">模板参数</div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="模板名称" required>
              <Input
                value={form.name}
                onChange={(value) => set("name", value === null ? "" : String(value))}
                placeholder="如：马来"
                maxLength={64}
              />
            </Field>
            <Field label="站点">
              <Input
                value={form.site}
                onChange={(value) => set("site", value === null ? "" : String(value))}
                placeholder="MY"
                maxLength={8}
              />
            </Field>
            <Field label="采购币种">
              <Input
                value={form.currency}
                onChange={(value) => set("currency", value === null ? "" : String(value))}
                placeholder="CNY"
                maxLength={8}
              />
            </Field>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="利润方式">
              <Select
                value={form.profitType}
                onChange={(value) => set("profitType", value)}
                options={[
                  { value: "bySalePriceProfit", label: "按售价利润率" },
                  { value: "byCostProfit", label: "按成本利润率" },
                ]}
              />
            </Field>
            <Field label="利润率 %">
              <NumberInput
                value={form.profitPercent}
                onChange={(value) => set("profitPercent", value === null ? "" : String(value))}
              />
            </Field>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="尾程运费 CNY">
              <NumberInput
                value={form.lastMileFreightCny}
                onChange={(value) => set("lastMileFreightCny", value === null ? "" : String(value))}
              />
            </Field>
            <Field label="平台佣金 %" hint="留空为 0（新店支持）">
              <NumberInput
                value={form.platformCommissionPercent}
                onChange={(value) => set("platformCommissionPercent", value === null ? "" : String(value))}
                placeholder="留空为 0"
              />
            </Field>
          </div>
        </div>

        {/* ---------- 一般无需修改 ---------- */}
        <div className="rounded-md border border-border">
          <button
            type="button"
            onClick={() => setAdvancedOpen((prev) => !prev)}
            className="flex w-full items-center justify-between px-3 py-2 text-left text-xs font-medium text-fg-secondary transition-colors hover:bg-black/[0.03]"
          >
            <span>一般无需修改</span>
            <ChevronDown
              className={`size-3.5 transition-transform ${advancedOpen ? "rotate-180" : ""}`}
            />
          </button>
          {advancedOpen && (
            <div className="grid grid-cols-1 gap-3 border-t border-border p-3 sm:grid-cols-3">
              <Field label="支付手续费 %">
                <NumberInput
                  value={form.paymentChargePercent}
                  onChange={(value) => set("paymentChargePercent", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="VAT %">
                <NumberInput
                  value={form.vatChargePercent}
                  onChange={(value) => set("vatChargePercent", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="提现手续费 %">
                <NumberInput
                  value={form.withdrawChargePercent}
                  onChange={(value) => set("withdrawChargePercent", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="BCP %">
                <NumberInput
                  value={form.bcpChargePercent}
                  onChange={(value) => set("bcpChargePercent", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="SFP %" hint="留空为 0">
                <NumberInput
                  value={form.sfpChargePercent}
                  onChange={(value) => set("sfpChargePercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="联盟佣金 %" hint="留空为 0">
                <NumberInput
                  value={form.affiliateChargePercent}
                  onChange={(value) => set("affiliateChargePercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="平台补贴 CNY">
                <NumberInput
                  value={form.platformSupportCny}
                  onChange={(value) => set("platformSupportCny", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="尾程服务税 %" hint="留空为 0">
                <NumberInput
                  value={form.lastMileFreightServiceTaxPercent}
                  onChange={(value) => set("lastMileFreightServiceTaxPercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="其它费用 CNY" hint="留空为 0">
                <NumberInput
                  value={form.otherChargeCny}
                  onChange={(value) => set("otherChargeCny", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="首重 g">
                <NumberInput
                  value={form.firstWeightGrams}
                  onChange={(value) => set("firstWeightGrams", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="首重价 CNY">
                <NumberInput
                  value={form.firstWeightPrice}
                  onChange={(value) => set("firstWeightPrice", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="续重单位 g">
                <NumberInput
                  value={form.continuedUnitGrams}
                  onChange={(value) => set("continuedUnitGrams", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="续重单位价 CNY">
                <NumberInput
                  value={form.continuedUnitPrice}
                  onChange={(value) => set("continuedUnitPrice", value === null ? "" : String(value))}
                />
              </Field>
            </div>
          )}
        </div>

        {error && <InlineError>{error}</InlineError>}
        {templatesQuery.isError && (
          <InlineError>{apiErrorMessage(templatesQuery.error)}</InlineError>
        )}
      </div>
    </Dialog>
  );
}
