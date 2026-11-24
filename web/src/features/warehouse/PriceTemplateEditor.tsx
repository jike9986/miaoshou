// features/warehouse —— 定价模板编辑器（可复用实体）
//
// 同一个编辑器被两处复用：
//   · `PriceTemplateDialog`（自动化页的弹窗，边处理边改模板）
//   · `features/settings/PricingTemplatePanel`（设置页内联，常驻编辑）
// 实体只有这一份 —— 参数列表写两遍必然漂移，之前就是因为只在一处加字段，
// 另一个入口改不了，用户看到的价一直对不上。
//
// 布局对齐妙手 ERP 的「定价模板」编辑页（负责人 2026-11-23 明确）：
//   左边是模板参数（模板信息 / 利润信息 / 成本信息 / 平台费用 / 尾数处理），
//   右边常驻「售价试算」，填采购价 + 重量就能立刻看到算出来的价和费用明细。
//
// 字段名与后端 `price_template.DEFAULT_TEMPLATE` 一一对应，不做重命名 ——
// `normalize_template` 不认识的键会被静默丢掉，改名就等于该字段永远不生效。
//
// 为什么要把 22 个字段全摆出来：后端一直支持它们，但前端原来只暴露 8 个，
// 剩下的（折扣、关税、订单处理费、企业代扣税、首重续重…）改不了，
// 用户只能看到「算出来的价和官方试算差一截」却无处可调。

import { useEffect, useMemo, useState } from "react";
import { ChevronDown, Calculator, RefreshCw, Save, Trash2, Plus } from "lucide-react";
import { apiErrorMessage } from "@/lib/api";
import {
  useDeleteTemplate,
  useSaveTemplate,
  useSyncPriceTemplates,
  useTemplatePricePreview,
  useTemplates,
} from "@/lib/queries";
import {
  Button,
  Field,
  InlineError,
  Input,
  NumberInput,
  Select,
  Separator,
  toast,
} from "@/components/ui";

/** 站点 → 币种。妙手界面上站点一选，币种就跟着定了。 */
const SITE_CURRENCY: Record<string, string> = {
  MY: "MYR",
  TH: "THB",
  VN: "VND",
  PH: "PHP",
  SG: "SGD",
  ID: "IDR",
};

const SITE_OPTIONS = [
  { value: "MY", label: "马来西亚" },
  { value: "TH", label: "泰国" },
  { value: "VN", label: "越南" },
  { value: "PH", label: "菲律宾" },
  { value: "SG", label: "新加坡" },
  { value: "ID", label: "印尼" },
];

/** 与后端 DEFAULT_TEMPLATE 同名的可编辑字段（全部 22 项）。 */
export interface TemplateForm {
  name: string;
  site: string;
  currency: string;
  profitType: string;
  profitPercent: string;
  discount: string;
  freightMode: string;
  lastMileFreightCny: string;
  weightSource: string;
  // 平台费用
  platformCommissionPercent: string;
  paymentChargePercent: string;
  vatChargePercent: string;
  withdrawChargePercent: string;
  sfpChargePercent: string;
  affiliateChargePercent: string;
  bcpChargePercent: string;
  platformSupportCny: string;
  lastMileFreightServiceTaxPercent: string;
  otherChargeCny: string;
  activityChargePercent: string;
  tariffChargePercent: string;
  freeShippingChargePercent: string;
  eCommerceGrowthChargePercent: string;
  consumptionTaxChargePercent: string;
  localConsumptionTaxChargePercent: string;
  orderProcessCny: string;
  enterpriseWithholdPercent: string;
  // 首重 / 续重
  firstWeightGrams: string;
  firstWeightPrice: string;
  continuedUnitGrams: string;
  continuedUnitPrice: string;
}

const EMPTY_FORM: TemplateForm = {
  name: "",
  site: "MY",
  currency: "MYR",
  profitType: "bySalePriceProfit",
  profitPercent: "20",
  discount: "",
  freightMode: "officialCrossBorder",
  lastMileFreightCny: "16.63",
  weightSource: "spu",
  platformCommissionPercent: "",
  paymentChargePercent: "3.78",
  vatChargePercent: "10",
  withdrawChargePercent: "1",
  sfpChargePercent: "",
  affiliateChargePercent: "",
  bcpChargePercent: "3.24",
  platformSupportCny: "0.83",
  lastMileFreightServiceTaxPercent: "",
  otherChargeCny: "",
  activityChargePercent: "0",
  tariffChargePercent: "0",
  freeShippingChargePercent: "0",
  eCommerceGrowthChargePercent: "0",
  consumptionTaxChargePercent: "0",
  localConsumptionTaxChargePercent: "0",
  orderProcessCny: "",
  enterpriseWithholdPercent: "0",
  firstWeightGrams: "30",
  firstWeightPrice: "0.18",
  continuedUnitGrams: "10",
  continuedUnitPrice: "0.12",
};

/** 空串 -> null（后端把 null 当「不参与计算」），有值则原样交给后端校验。 */
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
    discount: toNumberOrNull(form.discount),
    freightMode: form.freightMode,
    lastMileFreightCny: toNumberOrNull(form.lastMileFreightCny),
    weightSource: form.weightSource,
    platformCommissionPercent: toNumberOrNull(form.platformCommissionPercent),
    paymentChargePercent: toNumberOrNull(form.paymentChargePercent),
    vatChargePercent: toNumberOrNull(form.vatChargePercent),
    withdrawChargePercent: toNumberOrNull(form.withdrawChargePercent),
    sfpChargePercent: toNumberOrNull(form.sfpChargePercent),
    affiliateChargePercent: toNumberOrNull(form.affiliateChargePercent),
    bcpChargePercent: toNumberOrNull(form.bcpChargePercent),
    platformSupportCny: toNumberOrNull(form.platformSupportCny),
    lastMileFreightServiceTaxPercent: toNumberOrNull(form.lastMileFreightServiceTaxPercent),
    otherChargeCny: toNumberOrNull(form.otherChargeCny),
    activityChargePercent: toNumberOrNull(form.activityChargePercent),
    tariffChargePercent: toNumberOrNull(form.tariffChargePercent),
    freeShippingChargePercent: toNumberOrNull(form.freeShippingChargePercent),
    eCommerceGrowthChargePercent: toNumberOrNull(form.eCommerceGrowthChargePercent),
    consumptionTaxChargePercent: toNumberOrNull(form.consumptionTaxChargePercent),
    localConsumptionTaxChargePercent: toNumberOrNull(form.localConsumptionTaxChargePercent),
    orderProcessCny: toNumberOrNull(form.orderProcessCny),
    enterpriseWithholdPercent: toNumberOrNull(form.enterpriseWithholdPercent),
    freightTable: {
      firstWeightGrams: toNumberOrNull(form.firstWeightGrams),
      firstWeightPrice: toNumberOrNull(form.firstWeightPrice),
      continuedUnitGrams: toNumberOrNull(form.continuedUnitGrams),
      continuedUnitPrice: toNumberOrNull(form.continuedUnitPrice),
    },
  };
}

/** 把后端模板对象摊平回表单（缺的字段留空，不臆造默认值）。 */
function formFromTemplate(
  raw: Record<string, unknown> | undefined,
  fallbackName: string
): TemplateForm {
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
    discount: text(raw.discount),
    freightMode: text(raw.freightMode) || "officialCrossBorder",
    lastMileFreightCny: text(raw.lastMileFreightCny),
    weightSource: text(raw.weightSource) || "spu",
    platformCommissionPercent: text(raw.platformCommissionPercent),
    paymentChargePercent: text(raw.paymentChargePercent),
    vatChargePercent: text(raw.vatChargePercent),
    withdrawChargePercent: text(raw.withdrawChargePercent),
    sfpChargePercent: text(raw.sfpChargePercent),
    affiliateChargePercent: text(raw.affiliateChargePercent),
    bcpChargePercent: text(raw.bcpChargePercent),
    platformSupportCny: text(raw.platformSupportCny),
    lastMileFreightServiceTaxPercent: text(raw.lastMileFreightServiceTaxPercent),
    otherChargeCny: text(raw.otherChargeCny),
    activityChargePercent: text(raw.activityChargePercent),
    tariffChargePercent: text(raw.tariffChargePercent),
    freeShippingChargePercent: text(raw.freeShippingChargePercent),
    eCommerceGrowthChargePercent: text(raw.eCommerceGrowthChargePercent),
    consumptionTaxChargePercent: text(raw.consumptionTaxChargePercent),
    localConsumptionTaxChargePercent: text(raw.localConsumptionTaxChargePercent),
    orderProcessCny: text(raw.orderProcessCny),
    enterpriseWithholdPercent: text(raw.enterpriseWithholdPercent),
    firstWeightGrams: text(table.firstWeightGrams),
    firstWeightPrice: text(table.firstWeightPrice),
    continuedUnitGrams: text(table.continuedUnitGrams),
    continuedUnitPrice: text(table.continuedUnitPrice),
  };
}

/** 费用明细一行的标签（后端 lines 的键）。 */
const LINE_LABELS: Record<string, string> = {
  purchase: "产品采购价",
  firstMile: "头程运费",
  lastMile: "尾程运费",
  platformCommission: "平台佣金",
  paymentCharge: "交易手续费",
  vatCharge: "增值税",
  consumptionTax: "消费税",
  localConsumptionTax: "本地消费税",
  tariffCharge: "关税",
  withdrawCharge: "提现手续费",
  sfpCharge: "SFP 服务费",
  affiliateCharge: "达人佣金",
  bcpCharge: "BCP 活动费",
  activityCharge: "活动费",
  freeShippingCharge: "包邮服务费",
  eCommerceGrowthCharge: "电商增长服务费",
  platformSupport: "平台支持费",
  lastMileFreightServiceTax: "尾程运费服务税",
  orderProcess: "订单处理服务费",
  enterpriseWithhold: "企业商家代扣税",
  otherCharge: "其他费用",
};

export interface PriceTemplateEditorProps {
  /** 编辑器是否可见。不可见时不拉模板列表（设置页常驻传 true） */
  active?: boolean;
  /** 保存 / 删除按钮放在编辑器内部还是交给外部（弹窗用外部 footer） */
  showActions?: boolean;
  /** 保存成功后的回调（弹窗用它关窗，面板用它提示） */
  onSaved?: (name: string) => void;
}

/**
 * 定价模板编辑器实体：左参数、右试算，顶部切换模板。
 * 不带 Dialog、不带页头 —— 由调用方决定外面包什么。
 */
export function PriceTemplateEditor({
  active = true,
  showActions = true,
  onSaved,
}: PriceTemplateEditorProps) {
  const templatesQuery = useTemplates(active);
  const saveTemplate = useSaveTemplate();
  const deleteTemplate = useDeleteTemplate();
  const syncTemplates = useSyncPriceTemplates();
  const pricePreview = useTemplatePricePreview();

  const [form, setForm] = useState<TemplateForm>(EMPTY_FORM);
  const [picked, setPicked] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(true);
  // 试算入参 + 结果
  const [purchaseCny, setPurchaseCny] = useState("");
  const [weightKg, setWeightKg] = useState("");
  const [rate, setRate] = useState("");
  const [preview, setPreview] = useState<
    import("@/lib/queries").TemplatePricePreviewResponse | null
  >(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const templates = templatesQuery.data?.templates ?? [];
  const templateOptions = useMemo(
    () => [
      { value: "", label: "＋ 新建模板" },
      ...templates.map((item) => ({
        value: item.name,
        label: item.invalid ? `${item.name}（不可用）` : item.name,
      })),
    ],
    [templates]
  );

  // 首次显示时默认选中第一个模板，把它填进表单
  useEffect(() => {
    if (!active) return;
    if (picked) return;
    const first = templates.find((item) => !item.invalid) ?? templates[0];
    if (first) setPicked(first.name);
  }, [active, picked, templates]);

  // 选中项变化 -> 回填表单
  useEffect(() => {
    if (!active || !picked) return;
    const found = templates.find((item) => item.name === picked);
    if (found?.template) {
      setForm(formFromTemplate(found.template as Record<string, unknown>, found.name));
    }
  }, [active, picked, templates]);

  const set = <K extends keyof TemplateForm>(key: K, value: TemplateForm[K]): void => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  /** 选站点时把币种一起换掉 —— 妙手就是这么联动的。 */
  const pickSite = (site: string): void => {
    setForm((prev) => ({
      ...prev,
      site,
      currency: SITE_CURRENCY[site] ?? prev.currency,
    }));
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
        onSaved?.(name);
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

  /** 试算：用表单当前值算 —— 改完参数不用先保存就能看结果。 */
  const calculate = (): void => {
    if (!purchaseCny.trim() || !weightKg.trim()) {
      setPreviewError("请填写产品采购价和产品包裹重量");
      return;
    }
    setPreviewError(null);
    pricePreview.mutate(
      {
        purchaseCny: purchaseCny.trim(),
        weightKg: weightKg.trim(),
        rate: rate.trim() || null,
        templateName: form.name.trim() || picked || null,
        site: form.site,
      },
      {
        onSuccess: (data) => setPreview(data),
        onError: (err) => {
          setPreview(null);
          setPreviewError(apiErrorMessage(err));
        },
      }
    );
  };

  const lines = preview?.lines ?? {};
  const currency = String(preview?.currency ?? form.currency ?? "");
  const price = preview?.price;

  return (
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        {/* ================= 左：模板参数 ================= */}
        <div className="flex flex-col gap-4">
          {/* ---------- 模板选择 ---------- */}
          <div className="flex flex-col gap-2">
            <div className="text-xs font-medium text-fg-secondary">已存模板</div>
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
                variant="ghost"
                icon={<Plus />}
                onClick={() => {
                  setPicked("");
                  setForm(EMPTY_FORM);
                }}
              >
                新建
              </Button>
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

          {/* ---------- 模板信息 ---------- */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="模板名称" required>
              <Input
                value={form.name}
                onChange={(value) => set("name", value === null ? "" : String(value))}
                placeholder="如：马来"
                maxLength={64}
              />
            </Field>
            <Field label="站点">
              <Select value={form.site} onChange={pickSite} options={SITE_OPTIONS} />
            </Field>
            <Field label="币种" hint="随站点自动带出">
              <Input
                value={form.currency}
                onChange={(value) => set("currency", value === null ? "" : String(value))}
                placeholder="MYR"
                maxLength={8}
              />
            </Field>
            <Field label="汇率" hint="1 CNY = ? 站点币种；留空用兜底汇率">
              <Input
                value={rate}
                onChange={(value) => setRate(value === null ? "" : String(value))}
                placeholder="留空自动获取"
              />
            </Field>
          </div>

          {/* ---------- 利润信息 ---------- */}
          <div className="flex flex-col gap-3 rounded-md border border-border p-3">
            <div className="text-xs font-semibold text-fg-secondary">利润信息</div>
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
              <Field label="利润率 %" required>
                <NumberInput
                  value={form.profitPercent}
                  onChange={(value) => set("profitPercent", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="折扣" hint="1~9.99 折；留空不打折">
                <NumberInput
                  value={form.discount}
                  onChange={(value) => set("discount", value === null ? "" : String(value))}
                  placeholder="留空不打折"
                />
              </Field>
            </div>
          </div>

          {/* ---------- 成本信息 ---------- */}
          <div className="flex flex-col gap-3 rounded-md border border-border p-3">
            <div className="text-xs font-semibold text-fg-secondary">成本信息</div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="头程运费">
                <Select
                  value={form.freightMode}
                  onChange={(value) => set("freightMode", value)}
                  options={[
                    { value: "officialCrossBorder", label: "使用官方跨境物流（跨境物流小包）" },
                    { value: "selfFreight", label: "商家自行承担" },
                  ]}
                />
              </Field>
              <Field label="尾程运费" required hint="固定 CNY 金额">
                <NumberInput
                  value={form.lastMileFreightCny}
                  onChange={(value) => set("lastMileFreightCny", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="重量取值">
                <Select
                  value={form.weightSource}
                  onChange={(value) => set("weightSource", value)}
                  options={[
                    { value: "spu", label: "产品包裹重量" },
                    { value: "sku", label: "SKU 重量" },
                  ]}
                />
              </Field>
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
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
          </div>

          {/* ---------- 平台费用 ---------- */}
          <div className="flex flex-col gap-3 rounded-md border border-border p-3">
            <div className="text-xs font-semibold text-fg-secondary">平台费用</div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Field label="平台佣金率 %">
                <NumberInput
                  value={form.platformCommissionPercent}
                  onChange={(value) => set("platformCommissionPercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="交易手续费率 %">
                <NumberInput
                  value={form.paymentChargePercent}
                  onChange={(value) => set("paymentChargePercent", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="增值税率 %">
                <NumberInput
                  value={form.vatChargePercent}
                  onChange={(value) => set("vatChargePercent", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="消费税率 %">
                <NumberInput
                  value={form.consumptionTaxChargePercent}
                  onChange={(value) => set("consumptionTaxChargePercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="本地消费税率 %">
                <NumberInput
                  value={form.localConsumptionTaxChargePercent}
                  onChange={(value) => set("localConsumptionTaxChargePercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="关税税率 %">
                <NumberInput
                  value={form.tariffChargePercent}
                  onChange={(value) => set("tariffChargePercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="提现手续费率 %">
                <NumberInput
                  value={form.withdrawChargePercent}
                  onChange={(value) => set("withdrawChargePercent", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="SFP 服务费率 %">
                <NumberInput
                  value={form.sfpChargePercent}
                  onChange={(value) => set("sfpChargePercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="达人佣金费率 %">
                <NumberInput
                  value={form.affiliateChargePercent}
                  onChange={(value) => set("affiliateChargePercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="BCP 活动费率 %">
                <NumberInput
                  value={form.bcpChargePercent}
                  onChange={(value) => set("bcpChargePercent", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="活动费率 %">
                <NumberInput
                  value={form.activityChargePercent}
                  onChange={(value) => set("activityChargePercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="包邮服务费率 %">
                <NumberInput
                  value={form.freeShippingChargePercent}
                  onChange={(value) => set("freeShippingChargePercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="电商增长服务费率 %">
                <NumberInput
                  value={form.eCommerceGrowthChargePercent}
                  onChange={(value) => set("eCommerceGrowthChargePercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="企业商家代扣税率 %">
                <NumberInput
                  value={form.enterpriseWithholdPercent}
                  onChange={(value) => set("enterpriseWithholdPercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="尾程运费服务税率 %">
                <NumberInput
                  value={form.lastMileFreightServiceTaxPercent}
                  onChange={(value) => set("lastMileFreightServiceTaxPercent", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="平台支持费 CNY">
                <NumberInput
                  value={form.platformSupportCny}
                  onChange={(value) => set("platformSupportCny", value === null ? "" : String(value))}
                />
              </Field>
              <Field label="订单处理服务费 CNY">
                <NumberInput
                  value={form.orderProcessCny}
                  onChange={(value) => set("orderProcessCny", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
              <Field label="其它费用 CNY">
                <NumberInput
                  value={form.otherChargeCny}
                  onChange={(value) => set("otherChargeCny", value === null ? "" : String(value))}
                  placeholder="留空为 0"
                />
              </Field>
            </div>
          </div>

          {/* ---------- 高级（折叠） ---------- */}
          <div className="rounded-md border border-border">
            <button
              type="button"
              onClick={() => setAdvancedOpen((prev) => !prev)}
              className="flex w-full items-center justify-between px-3 py-2 text-left text-xs font-medium text-fg-secondary transition-colors hover:bg-surface-hover"
            >
              <span>参数速查</span>
              <ChevronDown
                className={`size-3.5 transition-transform ${advancedOpen ? "rotate-180" : ""}`}
              />
            </button>
            {advancedOpen && (
              <div className="flex flex-col gap-1.5 border-t border-border p-3 text-[11px] leading-relaxed text-fg-tertiary">
                <div>· 留空的费率按 0 计算，不是「未知」——想按官方价算，把税率填全。</div>
                <div>· 固定 CNY 金额项（尾程运费 / 平台支持费 / 订单处理费 / 其它）先换成本币再计入成本。</div>
                <div>· 折扣填 8.5 表示 8.5 折；留空不打折。</div>
                <div>· 改模板后已入仓商品的售价不会自动重算，要重新处理才会应用。</div>
              </div>
            )}
          </div>

          {error && <InlineError>{error}</InlineError>}
          {templatesQuery.isError && (
            <InlineError>{apiErrorMessage(templatesQuery.error)}</InlineError>
          )}
        </div>

        {/* ================= 右：售价试算 ================= */}
        <div className="flex h-fit flex-col gap-3 rounded-md border border-border bg-surface-sunken p-3 lg:sticky lg:top-0">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-fg-secondary">
            <Calculator className="size-3.5" />
            售价试算
          </div>

          <Field label="产品采购价" required>
            <NumberInput
              value={purchaseCny}
              onChange={(value) => setPurchaseCny(value === null ? "" : String(value))}
              placeholder="CNY"
            />
          </Field>
          <Field label="产品包裹重量" required>
            <NumberInput
              value={weightKg}
              onChange={(value) => setWeightKg(value === null ? "" : String(value))}
              placeholder="KG"
            />
          </Field>

          <div className="flex gap-2">
            <Button
              variant="secondary"
              size="sm"
              className="flex-1"
              onClick={() => {
                setPurchaseCny("");
                setWeightKg("");
                setPreview(null);
                setPreviewError(null);
              }}
            >
              重置
            </Button>
            <Button
              variant="primary"
              size="sm"
              className="flex-1"
              loading={pricePreview.isPending}
              onClick={calculate}
            >
              计算
            </Button>
          </div>

          {previewError && <InlineError>{previewError}</InlineError>}

          {preview && (
            <div className="flex flex-col gap-2 text-xs">
              <div className="flex items-center justify-between rounded border border-border bg-surface px-2 py-1.5">
                <span className="text-fg-secondary">税前售价</span>
                <span className="font-semibold text-fg">
                  {typeof price === "number" ? price.toFixed(2) : String(price ?? "-")}{" "}
                  {currency}
                </span>
              </div>
              {preview.isEstimate ? (
                <div className="rounded border border-warning-border bg-warning-soft px-2 py-1.5 text-[11px] text-warning">
                  这是估算值：{preview.rateSource}
                  {preview.missingRateFields.length
                    ? `，缺 ${preview.missingRateFields.join(" / ")}`
                    : ""}
                </div>
              ) : null}
              <div className="text-[11px] font-medium text-fg-tertiary">详细费用（CNY）</div>
              <div className="flex flex-col gap-1">
                {Object.entries(lines).map(([key, value]) => (
                  <div key={key} className="flex items-center justify-between gap-2">
                    <span className="text-fg-secondary">{LINE_LABELS[key] ?? key}</span>
                    <span className="tabular-nums text-fg">
                      {typeof value === "number" ? value.toFixed(2) : String(value)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {showActions && (
          <div className="col-span-full flex justify-end gap-2 border-t border-border pt-4">
            <Button
              variant="primary"
              icon={<Save />}
              loading={saveTemplate.isPending}
              onClick={save}
            >
              保存模板
            </Button>
          </div>
        )}
      </div>
  );
}
