// features/publish —— 发布确认弹窗（api.md §8.1）
// 三重保险：显式商品列表 + 目标店铺 + 「confirm: true」二次勾选。
// 提交成功后不关窗：结果区常驻展示 repriced 明细表与 warning 告警块（可回看），
// 直到用户主动关闭弹窗；「刷新回执」直接放在结果区，不再只靠 8 秒 toast。

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, PackageCheck, RefreshCw, Send } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { apiErrorMessage } from "@/lib/api";
import { isLanguageMismatch, LANGUAGE_NAMES, toPublishItems } from "@/lib/constants";
import {
  useAuthStatus,
  usePublish,
  useRefreshPublishLogs,
  useShops,
  useTemplates,
  useSelectTemplate,
} from "@/lib/queries";
import type { PublishResponse, QualityReviewReason, QualityReviewStatus, RepricedEntry } from "@/lib/types";
import {
  Badge,
  Button,
  Checkbox,
  Dialog,
  Field,
  InlineError,
  Input,
  Select,
  Separator,
  SkeletonRows,
  Switch,
  toast,
} from "@/components/ui";

export interface PublishDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 仓库来源的商品 id（items[].warehouseId） */
  warehouseIds: string[];
  qualityReviewById?: Record<string, {
    status?: QualityReviewStatus;
    reasons: QualityReviewReason[];
  }>;
  /** 当前店铺（顶部下拉）作为默认目标店铺 */
  defaultShopId: string | null;
  /** 是否允许把仓库商品认领到采集箱；学员只能发布已认领商品 */
  allowClaimOnly?: boolean;
  /** 每件商品自己的来源 shopId，用来判断跨店铺发布（要建载体） */
  sourceShopIds?: Record<string, string>;
  connected: boolean;
  onSubmitted?: (result: PublishResponse) => void;
}

export function PublishDialog({
  open,
  onOpenChange,
  warehouseIds,
  qualityReviewById = {},
  defaultShopId,
  allowClaimOnly = true,
  sourceShopIds,
  connected,
  onSubmitted,
}: PublishDialogProps) {
  const navigate = useNavigate();
  const { data: auth } = useAuthStatus();
  const shopsQuery = useShops(auth?.accountId, { enabled: open && connected });
  const templatesQuery = useTemplates(open && connected);
  const selectTemplate = useSelectTemplate();
  const publish = usePublish();
  const refreshLogs = useRefreshPublishLogs();

  const shops = useMemo(() => shopsQuery.data?.shops ?? [], [shopsQuery.data?.shops]);
  const templates = templatesQuery.data?.templates ?? [];
  const usableTemplates = templates.filter((item) => !item.invalid);
  const requiresTemplate = !allowClaimOnly;
  const hasValidTemplate = usableTemplates.some((item) => item.name === template);

  const [shopId, setShopId] = useState<string | null>(defaultShopId);
  const [template, setTemplate] = useState<string>("");
  // 汇率覆盖：留空则用商品自身的 originPrice/price 比值（老版 wh-price-rate）
  const [exchangeRate, setExchangeRate] = useState<string>("");
  const [language, setLanguage] = useState<string>("");
  const [claimOnly, setClaimOnly] = useState(false);
  const effectiveClaimOnly = allowClaimOnly && claimOnly;
  const [allowMismatch, setAllowMismatch] = useState(false);
  const [confirmedUnverifiedSpecifications, setConfirmedUnverifiedSpecifications] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 发布结果：非 null 时弹窗切到「结果区」，repriced / warning 常驻可回看 */
  const [result, setResult] = useState<PublishResponse | null>(null);

  // 跨店铺发布：目标店铺与商品来源店铺不同的件数。
  // 跨店要走载体（后端自动采集并创建），池子不够会直接失败，
  // 老版在弹窗里就把这件事讲清楚（wh-publish-cross），别让人到失败才知道。
  const crossCount = useMemo(() => {
    if (!shopId || !sourceShopIds) return 0;
    return warehouseIds.filter((id) => {
      const origin = sourceShopIds[id];
      return origin && String(origin) !== String(shopId);
    }).length;
  }, [shopId, sourceShopIds, warehouseIds]);

  // 汇率校验与后端一致：0 < rate <= 1000；留空合法
  const rateInvalid = (() => {
    const raw = exchangeRate.trim();
    if (!raw) return false;
    const parsed = Number(raw);
    return !Number.isFinite(parsed) || parsed <= 0 || parsed > 1000;
  })();

  // 打开时重置为默认店铺
  useEffect(() => {
    if (!open) return;
    setError(null);
    setConfirmed(false);
    setClaimOnly(false);
    setAllowMismatch(false);
    setConfirmedUnverifiedSpecifications(false);
    // 默认用该账号当前的定价模板（老版 refreshScheduleTemplates 的 result.current）。
    // 不填的话每次发布都得手动选一次，选漏了就是按原价上架。
    setTemplate(templatesQuery.data?.selected?.[String(auth?.accountId ?? "")] ?? "");
    setExchangeRate("");
    setLanguage("");
    setShopId(defaultShopId);
    setResult(null);
  }, [open, defaultShopId, auth?.accountId, templatesQuery.data?.selected]);

  // 店铺列表到手后校验默认值 / 语言默认取站点首语言
  useEffect(() => {
    if (!open || shops.length === 0) return;
    const target = shops.find((shop) => shop.shopId === shopId) ?? shops[0];
    if (target && target.shopId !== shopId) setShopId(target.shopId);
    if (target && !language) {
      const siteLanguages = shopsQuery.data?.siteLanguages?.[target.site] ?? target.languages;
      setLanguage(siteLanguages?.[0] ?? "");
    }
  }, [open, shops, shopId, language, shopsQuery.data?.siteLanguages]);

  const targetShop = shops.find((shop) => shop.shopId === shopId) ?? null;
  const siteLanguages =
    (targetShop && shopsQuery.data?.siteLanguages?.[targetShop.site]) ?? targetShop?.languages ?? [];
  const mismatch =
    Boolean(targetShop && language) &&
    Boolean(targetShop) &&
    isLanguageMismatch(targetShop?.site ?? "", language, shopsQuery.data?.siteLanguages);

  const languageOptions = siteLanguages.map((code) => ({
    value: code,
    label: LANGUAGE_NAMES[code] ?? code,
  }));

  const unverifiedQualityItems = warehouseIds
    .map((id) => ({ id, review: qualityReviewById[id] }))
    .filter(({ review }) => review?.status !== "verified");
  const requiresQualityAcknowledgement = requiresTemplate && unverifiedQualityItems.length > 0;

  const templateOptions = templates.map((item) => ({
    value: item.name,
    label: item.invalid ? `${item.name}（不可用）` : item.name,
    disabled: item.invalid,
  }));

  /** 回源刷新平台回执（结果区与 toast 共用） */
  const refreshReceipts = (): void => {
    refreshLogs.mutate(undefined, {
      onSuccess: (data) =>
        data.degraded
          ? toast.warning("平台暂时不可达，稍后再刷新")
          : toast.success(`已更新 ${data.updated} 条回执`),
      onError: (refreshError) =>
        toast.error("刷新回执失败", { description: apiErrorMessage(refreshError) }),
    });
  };

  const submit = (): void => {
    setError(null);
    if (warehouseIds.length === 0) {
      setError("请选择要发布的商品");
      return;
    }
    if (warehouseIds.length > 200) {
      setError("一次最多发布 200 件商品");
      return;
    }
    if (!shopId) {
      setError("请选择目标店铺");
      return;
    }
    if (requiresTemplate && !hasValidTemplate) {
      setError("学员发布必须选择一个可用的定价模板");
      return;
    }
    if (!confirmed) {
      setError("请勾选发布确认");
      return;
    }
    if (mismatch && !allowMismatch) {
      setError("所选语言与目标站点不匹配，请更换语言或勾选「接受语言不匹配」");
      return;
    }
    if (requiresQualityAcknowledgement && !confirmedUnverifiedSpecifications) {
      setError("所选商品包含待核验规格，请检查提醒并明确确认");
      return;
    }

    publish.mutate(
      {
        items: toPublishItems(warehouseIds.map((id) => ({ warehouseId: id }))),
        shopId,
        template: template || null,
        exchangeRate: exchangeRate.trim() || null,
        language: language || null,
        allowMismatch,
        confirmUnverifiedSpecifications: requiresQualityAcknowledgement
          && confirmedUnverifiedSpecifications,
        claimOnly: effectiveClaimOnly,
        // 三重保险：confirm 必须是字面量 true（api.md §8.1 CONFIRM_REQUIRED）
        confirm: true,
      },
      {
        onSuccess: (publishResult) => {
          // 不关窗：结果区（含 repriced 明细与 warning）保留到用户主动关闭
          setResult(publishResult);
          setError(null);
          onSubmitted?.(publishResult);
          const failed = publishResult.failures.length;
          if (failed > 0) {
            toast.warning(
              `已提交 ${publishResult.published.length} 件，${failed} 件失败`,
              { description: "已提交，平台回执稍后更新" }
            );
          } else {
            toast.success(
              effectiveClaimOnly
                ? `已认领 ${publishResult.claimed.length} 件进采集箱`
                : `已提交 ${publishResult.published.length} 件`,
              {
                description: "已提交，平台回执稍后更新",
                duration: 8000,
                action: {
                  label: "刷新回执",
                  onClick: refreshReceipts,
                },
              }
            );
          }
          // warning 仍给一次 toast 提醒，但真正的回看入口是结果区的常驻告警块
          if (publishResult.warning) {
            toast.warning("提示", { description: publishResult.warning, duration: 8000 });
          }
        },
        onError: (mutationError) => {
          setError(apiErrorMessage(mutationError));
          toast.error("发布失败", { description: apiErrorMessage(mutationError) });
        },
      }
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size={result ? "lg" : "md"}
      title={result ? "发布结果" : "发布到店铺"}
      description={
        result
          ? "结果保留在弹窗里，关闭后可在发布记录里回看。"
          : "发布不可撤销；提交后平台回执需要稍等，可在发布记录里刷新。"
      }
      closeOnOverlay={false}
      footer={
        result ? (
          <>
            <Button
              variant="secondary"
              onClick={() => {
                // 回到表单时把「我确认发布」重新置空，避免沿用上一次的确认态
                setResult(null);
                setConfirmed(false);
                setConfirmedUnverifiedSpecifications(false);
              }}
            >
              继续发布
            </Button>
            <Button
              variant="secondary"
              icon={<RefreshCw />}
              loading={refreshLogs.isPending}
              loadingText="回源中…"
              onClick={refreshReceipts}
            >
              刷新回执
            </Button>
            <Button variant="primary" onClick={() => onOpenChange(false)}>
              关闭
            </Button>
          </>
        ) : (
          <>
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              取消
            </Button>
            <Button
              variant="primary"
              icon={<Send />}
              loading={publish.isPending}
              loadingText="正在提交…"
              disabled={!confirmed || !shopId || rateInvalid
                || (requiresTemplate && !hasValidTemplate)
                || (requiresQualityAcknowledgement && !confirmedUnverifiedSpecifications)}
              onClick={submit}
            >
              {effectiveClaimOnly ? "只认领进采集箱" : `发布 ${warehouseIds.length} 件`}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <PublishResultPanel result={result} />
      ) : (
        <div className="flex flex-col gap-4">
          {!connected && (
            <InlineError>
              尚未连接妙手账号，发布前请先
              <button
                type="button"
                className="ml-1 font-medium underline"
                onClick={() => navigate("/unlock", { state: { from: "/publish" } })}
              >
                连接妙手
              </button>
              。
            </InlineError>
          )}

          <div className="rounded-md border border-border-subtle bg-canvas-inset/60 p-3">
            <div className="flex items-center justify-between">
              <p className="text-[13px] font-medium text-fg-secondary">发布清单</p>
              <Badge variant="neutral">
                <span className="num">{warehouseIds.length}</span> 件
              </Badge>
            </div>
            <p className="mt-1 font-mono text-2xs leading-4 text-fg-muted">
              {warehouseIds.slice(0, 12).join(", ")}
              {warehouseIds.length > 12 ? ` …等 ${warehouseIds.length} 件` : ""}
            </p>
          </div>

          {shopsQuery.isLoading ? (
            <SkeletonRows rows={3} />
          ) : shopsQuery.isError ? (
            <InlineError onRetry={() => void shopsQuery.refetch()}>
              {apiErrorMessage(shopsQuery.error)}
            </InlineError>
          ) : (
            <Field label="目标店铺" required hint={shopsQuery.data?.degraded ? "当前展示的是上次缓存的店铺列表" : undefined}>
              <Select
                value={shopId ?? ""}
                onChange={(value) => setShopId(value)}
                options={shops.map((shop) => ({
                  value: shop.shopId,
                  label: `${shop.name} · ${shop.siteName || shop.site}`,
                }))}
                placeholder={shops.length === 0 ? "当前账号下没有店铺" : "选择目标店铺"}
                disabled={shops.length === 0}
              />
            </Field>
          )}

          {(crossCount > 0 || requiresTemplate) && (
            <p className="rounded-md border border-info-500/20 bg-info-50 px-2.5 py-1.5 text-2xs leading-4 text-info-700">
              {requiresTemplate
                ? "学员发布会为商品创建发布载体，并按所选定价模板重算售价；链接池不足时发布会失败。"
                : `其中 ${crossCount} 件来自其他店铺，发布会自动采集并创建 ${crossCount} 个载体；链接池不足时发布会失败。`}
            </p>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              label="定价模板"
              required={requiresTemplate}
              hint={
                templatesQuery.isLoading
                  ? "正在读取定价模板…"
                  : templatesQuery.isError
                    ? undefined
                    : requiresTemplate && usableTemplates.length === 0
                      ? "暂无可用模板，请先在仓库的定价模板中创建并保存模板。"
                      : requiresTemplate
                        ? "发布时将按所选模板重算售价"
                        : templates.length === 0
                          ? "还没有保存定价模板（可留空）"
                          : "跨店铺发布时按模板重算售价"
              }
            >
              <Select
                value={template}
                onChange={(name) => {
                  setTemplate(name);
                  // 记住这个账号用哪套：下次打开自动带出来，建定时任务也用它当默认。
                  //
                  // 这里是「顺手记住」的副作用写入，用户的主操作是选模板、不是存模板，
                  // 所以失败**不能**把弹窗打断（他没要求保存，凭什么报错挡住他发布）。
                  // 但也**不能什么都不做**：原来这个 mutate 完全没有 onError，
                  // 失败时用户以为记住了，下次打开还是旧模板，而界面上一点提示都没有。
                  // 折中：toast 说一句「这次没记住，但发布用的是你刚选的」——
                  // 发布本身用的就是本地 state，不受这个写入影响。
                  if (name && auth?.accountId) {
                    selectTemplate.mutate(
                      { name, account: String(auth.accountId) },
                      {
                        onError: (error) => {
                          toast.warning("模板选择没保存住", {
                            description: `${apiErrorMessage(error)}。本次发布会用你刚选的「${name}」，但下次打开可能还是旧的。`,
                          });
                        },
                      }
                    );
                  }
                }}
                options={templateOptions}
                placeholder={requiresTemplate ? "选择定价模板" : "不使用模板"}
                disabled={templatesQuery.isLoading || (requiresTemplate && usableTemplates.length === 0)}
                required={requiresTemplate}
              />
              {templatesQuery.isError && (
                <InlineError onRetry={() => void templatesQuery.refetch()}>
                  定价模板读取失败：{apiErrorMessage(templatesQuery.error)}
                </InlineError>
              )}
            </Field>

            <Field
              label="汇率（1 CNY ≈ ? 目标币种）"
              hint="留空用商品自带的采购价/售价比值；填了覆盖它"
              error={rateInvalid ? "汇率需大于 0 且不超过 1000" : null}
            >
              <Input
                value={exchangeRate}
                onChange={(event) => setExchangeRate(event.target.value)}
                inputMode="decimal"
                placeholder="留空用商品汇率"
                invalid={rateInvalid}
              />
            </Field>

            <Field
              label="标题语言"
              hint={
                targetShop
                  ? `站点 ${targetShop.siteName || targetShop.site} 接受：${siteLanguages
                      .map((code) => LANGUAGE_NAMES[code] ?? code)
                      .join(" / ") || "未声明"}`
                  : "选择目标店铺后显示站点语言"
              }
              error={mismatch && !allowMismatch ? "语言与站点不匹配" : null}
            >
              <Select
                value={language}
                onChange={setLanguage}
                options={languageOptions}
                placeholder="不指定语言"
                disabled={languageOptions.length === 0}
                invalid={mismatch && !allowMismatch}
              />
            </Field>
          </div>

          {mismatch && (
            <div className="flex items-start gap-2 rounded-md border border-warning-600/20 bg-warning-50 px-3 py-2.5 text-[13px] leading-5 text-warning-700">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" strokeWidth={2} />
              <div className="min-w-0 flex-1">
                <p>所选语言与目标站点不匹配，默认会被后端拒绝（422 SITE_LANGUAGE_MISMATCH）。</p>
                <div className="mt-1.5">
                  <Switch
                    checked={allowMismatch}
                    onChange={setAllowMismatch}
                    label="接受语言不匹配（明确风险）"
                  />
                </div>
              </div>
            </div>
          )}

          {requiresQualityAcknowledgement && (
            <div className="flex items-start gap-2 rounded-md border border-warning-600/20 bg-warning-50 px-3 py-2.5 text-[13px] leading-5 text-warning-700">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" strokeWidth={2} />
              <div className="min-w-0 flex-1">
                <p>
                  {unverifiedQualityItems.length} 件商品的重量或尺寸仍待核验
                  {qualityReviewReasonsLabel(unverifiedQualityItems.flatMap(({ review }) => review?.reasons ?? []))}。
                  确认继续会按当前数据发布，不会将商品标记为已核验。
                </p>
                <div className="mt-1.5">
                  <Checkbox
                    checked={confirmedUnverifiedSpecifications}
                    onChange={setConfirmedUnverifiedSpecifications}
                    label="我已检查这些规格，仍要继续发布"
                  />
                </div>
              </div>
            </div>
          )}

          {allowClaimOnly && (
            <div className="rounded-md border border-border-subtle p-3">
              <Switch
                checked={claimOnly}
                onChange={setClaimOnly}
                label="只认领进采集箱，不上架"
              />
              <p className="mt-1.5 text-xs leading-[18px] text-fg-muted">
                勾选后调用 <code className="font-mono">claimOnly: true</code>，商品会进入目标店铺采集箱，仍需人工确认后发布。
              </p>
            </div>
          )}

          <Separator />

          <div className="rounded-md border border-danger-600/20 bg-danger-50/60 p-3">
            <p className="text-[13px] font-medium text-danger-700">发布不可撤销</p>
            <p className="mt-1 text-xs leading-[18px] text-danger-700/90">
              提交后商品会立即进入平台处理队列，失败也可能产生费用。请确认商品、店铺与语言都正确。
            </p>
            <div className="mt-2.5">
              <Checkbox
                checked={confirmed}
                onChange={setConfirmed}
                label="我确认发布，且知道不可撤销"
              />
            </div>
          </div>

          {error && <InlineError>{error}</InlineError>}
        </div>
      )}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ *
 * 结果区：repriced 明细表 + warning 告警块（常驻，直到用户关闭弹窗）
 * ------------------------------------------------------------------ */

const QUALITY_REVIEW_REASON_LABELS: Record<QualityReviewReason, string> = {
  missing_weight: "缺少有效重量",
  missing_dimensions: "缺少完整尺寸",
  default_dimensions: "尺寸仍是默认值",
  unverified: "尚无人工核验记录",
  stale_review: "商品变更后需要重新核验",
};

function qualityReviewReasonsLabel(reasons: QualityReviewReason[]): string {
  const labels = [...new Set(reasons.map((reason) => QUALITY_REVIEW_REASON_LABELS[reason]).filter(Boolean))];
  return labels.length > 0 ? `：${labels.join("、")}` : "";
}

function formatMoney(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return value;
}

function PublishResultPanel({ result }: { result: PublishResponse }) {
  const repriced: RepricedEntry[] = result.repriced ?? [];
  const failures = result.failures ?? [];

  return (
    <div className="flex flex-col gap-4">
      {/* warning：不再只弹 8 秒 toast，这里常驻可回看 */}
      {result.warning && (
        <div className="flex items-start gap-2 rounded-md border border-warning-600/25 bg-warning-50 px-3 py-2.5 text-[13px] leading-5 text-warning-700">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" strokeWidth={2} />
          <div className="min-w-0 flex-1">
            <p className="font-medium">发布提示</p>
            <p className="mt-0.5 break-words">{result.warning}</p>
          </div>
        </div>
      )}

      <div className="rounded-md border border-border-subtle bg-canvas-inset/60 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <PackageCheck className="size-4 text-success-600" strokeWidth={2} />
          <p className="text-[13px] font-medium text-fg-secondary">
            {result.publishRequested ? "已提交到平台" : "已处理"}
          </p>
          <Badge variant="neutral">
            批次 <span className="font-mono">{result.batchId || "—"}</span>
          </Badge>
          <Badge variant="info">模式 {result.mode}</Badge>
        </div>
        <div className="mt-2 grid grid-cols-3 gap-2">
          <div>
            <p className="text-2xs text-fg-muted">已发布</p>
            <p className="num text-sm font-semibold text-fg">{result.published.length}</p>
          </div>
          <div>
            <p className="text-2xs text-fg-muted">已认领</p>
            <p className="num text-sm font-semibold text-fg">{result.claimed.length}</p>
          </div>
          <div>
            <p className="text-2xs text-fg-muted">失败</p>
            <p
              className={cn(
                "num text-sm font-semibold",
                failures.length > 0 ? "text-danger-600" : "text-fg"
              )}
            >
              {failures.length}
            </p>
          </div>
        </div>
      </div>

      {repriced.length > 0 && (
        <div>
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <p className="text-[13px] font-semibold text-fg-secondary">模板改价明细</p>
            <Badge variant="warning">
              <span className="num">{repriced.length}</span> 件被重新定价
            </Badge>
          </div>
          <div className="overflow-hidden rounded-md border border-border-subtle">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="h-8 bg-canvas-inset/70 text-fg-muted">
                  <th className="px-2 text-left font-medium">商品</th>
                  <th className="w-[84px] px-2 text-right font-medium">改前价</th>
                  <th className="w-[84px] px-2 text-right font-medium">改后价</th>
                  <th className="w-[56px] px-2 text-left font-medium">币种</th>
                  <th className="w-[76px] px-2 text-right font-medium">重量</th>
                  <th className="w-[76px] px-2 text-right font-medium">改价 SKU</th>
                </tr>
              </thead>
              <tbody>
                {repriced.map((entry, index) => (
                  <tr
                    key={`${entry.detailId ?? entry.warehouseId ?? "row"}-${index}`}
                    className="border-t border-border-subtle"
                  >
                    <td className="max-w-[180px] truncate px-2 py-1.5 font-mono text-2xs text-fg-secondary">
                      {entry.detailId ?? entry.warehouseId ?? "—"}
                    </td>
                    <td className="num px-2 py-1.5 text-right text-fg-muted line-through">
                      {formatMoney(entry.priceBefore)}
                    </td>
                    <td className="num px-2 py-1.5 text-right font-medium text-fg">
                      {formatMoney(entry.priceAfter)}
                    </td>
                    <td className="px-2 py-1.5 text-fg-secondary">{entry.currency || "—"}</td>
                    <td className="num px-2 py-1.5 text-right text-fg-secondary">
                      {entry.weightKg ? `${entry.weightKg} kg` : "—"}
                    </td>
                    <td className="num px-2 py-1.5 text-right text-fg-secondary">
                      {entry.changed ?? "—"}
                      {entry.skuCount !== null && entry.skuCount !== undefined
                        ? `/${entry.skuCount}`
                        : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-1 text-2xs text-fg-muted">
            改前价为模板里取到的样本价；重量来自入库 payload，可能为空。
          </p>
        </div>
      )}

      {failures.length > 0 && (
        <div className="rounded-md border border-danger-600/20 bg-danger-50/60 px-3 py-2.5 text-[13px] leading-5 text-danger-700">
          <p className="font-medium">失败明细（{failures.length} 件）</p>
          <ul className="mt-1 flex flex-col gap-1">
            {failures.map((failure, index) => (
              <li key={`${failure.detailId ?? "fail"}-${index}`} className="break-words">
                <span className="font-mono text-2xs">{failure.detailId ?? "—"}</span>
                {failure.reason ? `：${failure.reason}` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}

      {repriced.length === 0 && failures.length === 0 && !result.warning && (
        <p className="text-[13px] leading-5 text-fg-muted">
          没有改价明细，也没有告警。平台回执稍后更新，可点「刷新回执」或到发布记录里查看。
        </p>
      )}
    </div>
  );
}

export default PublishDialog;
