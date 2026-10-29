// features/warehouse —— 商品详情抽屉（api.md §4.2 / §4.3）
// 展示图片、价格、类目与分组、发布历史、图片任务；支持就地改标题/类目/分组。

import { useEffect, useMemo, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { CalendarClock, PencilLine, Save, ScanSearch, Sparkles, Tag, Trash2 } from "lucide-react";
import { cn, formatCount, formatDate, formatPrice } from "@/lib/utils";
import { apiErrorMessage } from "@/lib/api";
import { isConflictBusy, revisionConflictOf, type RevisionConflict } from "@/lib/conflict";
import type { ImageAnalyzeItem } from "@/lib/queries";
import { LANGUAGE_NAMES, validateTitle } from "@/lib/constants";
import { useOperatorIdentity } from "@/lib/operator";
import { imageStatus, productStatus, publishStatus } from "@/features/shared/status";
import { RemoteImage } from "@/features/shared/RemoteImage";
import { ImageJobRow, ImageJobUploadSummary } from "@/features/shared/ImageJobRow";
import { RevisionConflictDialog } from "@/features/shared/RevisionConflictDialog";
import {
  useDeleteProduct,
  useGroups,
  usePatchProduct,
  useReviewProductQuality,
  useProductDetail,
  useAnalyzeImages,
  useTranslateImages,
  useImageUsage,
  useImageJobs,
  useReconcileImages,
  useTitlePreview,
  useAuthStatus,
} from "@/lib/queries";
import type { ProductDetailResponse } from "@/lib/types";
import { buildQualityReviewRequest } from "@/features/warehouse/qualityReviewInput";
import {
  Badge,
  Button,
  ConfirmDialog,
  Drawer,
  ErrorState,
  Field,
  IconButton,
  InlineError,
  Input,
  Select,
  Separator,
  SkeletonRows,
  StatusBadge,
  Tooltip,
  Textarea,
  toast,
} from "@/components/ui";

export interface ProductDetailDrawerProps {
  productId: string | null;
  open: boolean;
  isMember?: boolean;
  onOpenChange: (open: boolean) => void;
  onRequestDelete?: (id: string) => void;
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-[13px] font-semibold uppercase tracking-[0.04em] text-fg-muted">
      {children}
    </h3>
  );
}

function positiveMeasure(value: unknown): string | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? String(parsed) : null;
}

export function ProductDetailDrawer({
  productId,
  open,
  isMember = false,
  onOpenChange,
  onRequestDelete,
}: ProductDetailDrawerProps) {
  // includePayload=true：翻译检查报告存在 payload.translation.coverage 里
  const detailQuery = useProductDetail(open ? productId : null, true);
  const payload = detailQuery.data?.product.payload;
  const payloadSite = typeof payload?.site === "string" ? payload.site : "";
  const packageWeight = positiveMeasure(payload?.weight);
  const packageDimensions = [
    positiveMeasure(payload?.packageLength),
    positiveMeasure(payload?.packageWidth),
    positiveMeasure(payload?.packageHeight),
  ];
  const dimensionsComplete = packageDimensions.every((value) => value !== null);
  const groupsQuery = useGroups();
  const patch = usePatchProduct();
  const qualityReviewMutation = useReviewProductQuality();
  const remove = useDeleteProduct();
  const titlePreview = useTitlePreview();
  const analyze = useAnalyzeImages();
  const translate = useTranslateImages();
  const reconcileImages = useReconcileImages();
  // 逐张人工结论：老版 image-plan 能改 rows[].action，这里保持一致。
  // key 是图片 url，缺省按识别结果（translate 才默认译，其余保留）。
  const [planOverride, setPlanOverride] = useState<Record<string, "keep" | "translate">>({});

  const [title, setTitle] = useState("");
  const [groupId, setGroupId] = useState<string>("");
  const [qualityWeight, setQualityWeight] = useState("");
  const [qualityLength, setQualityLength] = useState("");
  const [qualityWidth, setQualityWidth] = useState("");
  const [qualityHeight, setQualityHeight] = useState("");
  const [qualitySource, setQualitySource] = useState("");
  const [qualityEvidence, setQualityEvidence] = useState("");
  const [qualityError, setQualityError] = useState<string | null>(null);
  const [titleError, setTitleError] = useState<string | null>(null);

  /**
   * 按词库清理标题：只把清理结果填回输入框，不直接保存。
   * 用户还需要点「保存」，避免误改真实标题（清理规则见 api.md §16.1）。
   */
  /** 这张图最终要不要译：人工改过就听人工的，否则听识别结果 */
  const decisionOf = (row: ImageAnalyzeItem): "keep" | "translate" =>
    planOverride[row.url] ?? (row.action === "translate" ? "translate" : "keep");

  /** 把人工结论提交给翻译服务（keep 的会被后端跳过，不占预算） */
  const translatePlanned = (): void => {
    const rows = analyze.data?.items ?? [];
    const picked = rows.filter((row) => decisionOf(row) === "translate");
    if (picked.length === 0) {
      toast.info("没有标记为翻译的图片");
      return;
    }
    translate.mutate(
      {
        items: picked.map((row) => ({
          // productId 必须带：不带的话任务不挂到商品上，
          // 详情里的「图片翻译任务」永远是「还没有图片翻译任务」
          productId: product?.id,
          detailId: product?.detailId,
          source: "main" as const,
          url: row.url,
          action: "translate" as const,
        })),
      },
      {
        onSuccess: () => toast.success(`已提交 ${picked.length} 张图翻译`),
        onError: (error) => {
          // 409 CONFLICT_BUSY：这件商品正在被别的任务占着，不是错误，等一会儿再来
          if (isConflictBusy(error)) {
            toast.warning(apiErrorMessage(error));
            return;
          }
          toast.error("提交翻译失败", { description: apiErrorMessage(error) });
        },
      }
    );
  };

  /** 主图前 N 张：识别有没有中文（本地 OCR，不花预算） */
  const runAnalyze = (): void => {
    if (mainImages.length === 0) return;
    analyze.mutate(
      { images: mainImages },
      {
        onError: (error) =>
          toast.error("图片识别失败", { description: apiErrorMessage(error) }),
      }
    );
  };

  const cleanTitle = (): void => {
    const current = title.trim();
    if (!current) {
      toast.warning("标题是空的，没有可清理的内容");
      return;
    }
    titlePreview.mutate(
      { titles: [current] },
      {
        onSuccess: (result) => {
          const first = result.items[0];
          if (!first || !first.changed) {
            toast.info("标题里没有命中违规词");
            return;
          }
          setTitle(first.cleaned);
          setTitleError(null);
          const words = first.removedWords.join("、");
          toast.success(`已按词库清理：删掉 ${words}`, {
            description: "确认无误后点「保存」写入，只改本地不回写妙手",
          });
        },
        onError: (error) =>
          toast.error("标题清理失败", { description: apiErrorMessage(error) }),
      }
    );
  };

  const refreshYears = (): void => {
    const current = title.trim();
    if (!current) {
      toast.warning("标题是空的，没有可刷新的内容");
      return;
    }
    // 先只查不换：告诉用户标题里到底有没有过时年份，别一上来就改。
    titlePreview.mutate(
      { titles: [current], refreshYears: false },
      {
        onSuccess: (probe) => {
          const stale = probe.items[0]?.staleYears ?? 0;
          if (stale === 0) {
            toast.info("标题里没有过时的年份");
            return;
          }
          titlePreview.mutate(
            { titles: [current], refreshYears: true },
            {
              onSuccess: (result) => {
                const first = result.items[0];
                if (!first || first.cleaned === current) {
                  toast.info("年份已是今年，无需修改");
                  return;
                }
                setTitle(first.cleaned);
                setTitleError(null);
                toast.success(`已把 ${stale} 处过时年份换成今年`, {
                  description: "确认无误后点「保存」写入，只改本地不回写妙手",
                });
              },
              onError: (error) =>
                toast.error("年份刷新失败", { description: apiErrorMessage(error) }),
            }
          );
        },
        onError: (error) =>
          toast.error("标题检查失败", { description: apiErrorMessage(error) }),
      }
    );
  };

  const data: ProductDetailResponse | undefined = detailQuery.data;

  /**
   * 逐字段翻译覆盖报告（后端 services/translation_coverage.py 生成，
   * 存在 product.payload.translation.coverage）。老版对应 coverage-heading 区块。
   */
  const coverage = (() => {
    const payload = data?.product?.payload as
      | Record<string, unknown>
      | null
      | undefined;
    const translation = (payload?.translation ?? null) as
      | Record<string, unknown>
      | null;
    const raw = (translation?.coverage ?? null) as Record<string, unknown> | null;
    if (!raw) return null;
    return {
      complete: raw.complete === true,
      targetLanguage: String(raw.targetLanguage ?? ""),
      missingFields: Array.isArray(raw.missingFields)
        ? (raw.missingFields as unknown[]).map(String)
        : [],
      // 逐字段状态（老版 renderCoverage）：只知道「缺几项」不够，
      // 还要知道哪些是保留原样、哪些根本没提供，核对时才分得清。
      requestedFields:
        raw.requestedFields && typeof raw.requestedFields === "object"
          ? Object.entries(raw.requestedFields as Record<string, unknown>).map(
              ([path, status]) => [path, String(status)] as const
            )
          : [],
    };
  })();
  const product = data?.product;

  // 换商品时清空分析结果和人工结论：
  // analyze.data 是 mutation 状态，不会随商品切换自动清——
  // 打开 A 的分析结果，再打开 B 会看到 A 的图。
  useEffect(() => {
    setPlanOverride({});
    analyze.reset();
  }, [product?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // 额度只在抽屉打开时查一次，别为每个商品都发请求
  const usage = useImageUsage({ enabled: Boolean(product?.id) });
  // 完整任务列表：详情接口的 imageJobs 没有 publicUrl / uploadError，
  // 「公网链接」和「上传失败原因」只能从这里拿
  const fullJobs = useImageJobs(
    { productId: product?.id ?? null, pageSize: 20 },
    { enabled: Boolean(product?.id) }
  );
  // 只读模式下后端会拒掉上传（require_writable），按钮直接禁用并说明原因
  const authStatus = useAuthStatus();
  const identity = useOperatorIdentity();
  const memberView = isMember || identity?.role === "member";
  const canWrite = !memberView && authStatus.data?.readOnly !== true;
  const canManageQuality = canWrite &&
    (!identity || identity.role === "admin" || identity.role === "teacher");

  useEffect(() => {
    if (!product) return;
    setTitle(product.title);
    setGroupId(product.groupId ?? "");
    setTitleError(null);
    setQualityWeight(positiveMeasure(payload?.weight) ?? "");
    setQualityLength(positiveMeasure(payload?.packageLength) ?? "");
    setQualityWidth(positiveMeasure(payload?.packageWidth) ?? "");
    setQualityHeight(positiveMeasure(payload?.packageHeight) ?? "");
    setQualitySource(product.qualityReview?.source ?? "");
    setQualityEvidence(product.qualityReview?.evidence ?? "");
    setQualityError(null);
  }, [product, payload?.weight, payload?.packageLength,
    payload?.packageWidth, payload?.packageHeight]);

  const mainImages = useMemo(() => {
    const list = (product?.images ?? []) as string[];
    return list.filter((url) => typeof url === "string" && url.startsWith("http"));
  }, [product?.images]);

  const dirty = Boolean(product) && (title !== product?.title || groupId !== (product?.groupId ?? ""));

  /* ---------------- 乐观并发（revision） ----------------
   * 规则：
   *   · 打开抽屉 / 换商品时把当时的 revision 记下来，保存时作为 expectedRevision 发出；
   *   · 后端比对不一致就回 409 REVISION_CONFLICT，弹窗让人选「刷新」还是「覆盖」；
   *   · 「保留我的修改」用后端给的 actual 当新的 expectedRevision 再发一次（显式覆盖）；
   *   · 老后端没有 revision 字段 → 不传 expectedRevision，行为与升级前一致。
   */
  const revisionRef = useRef<number | undefined>(undefined);
  // 冲突时把待提交的内容存下来：点「保留我的修改」要原样重发，不能让用户重新输一遍
  const pendingPatchRef = useRef<{ title?: string; groupId?: string | null } | null>(null);
  const [conflict, setConflict] = useState<RevisionConflict | null>(null);

  // 只在「换了一件商品」时重置基准版本：同一件商品的重复拉取不覆盖，
  // 否则别人改完一刷新，基准版本就被悄悄抬到最新，「保留我的修改」也就失去意义。
  useEffect(() => {
    if (!product) return;
    revisionRef.current = product.revision;
  }, [product?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  /** 真正发请求的地方：save 与「保留我的修改」共用，只有 expectedRevision 不同 */
  const submitPatch = (
    body: { title?: string; groupId?: string | null },
    expectedRevision: number | undefined
  ): void => {
    if (!product) return;
    const payload =
      expectedRevision === undefined ? body : { ...body, expectedRevision };
    patch.mutate(
      { id: product.id, patch: payload },
      {
        onSuccess: (result) => {
          // 保存成功：基准版本推进到后端返回的新版本，否则下一次保存会立刻自我冲突
          const nextRevision = result.product.revision;
          if (typeof nextRevision === "number") revisionRef.current = nextRevision;
          pendingPatchRef.current = null;
          toast.success("已保存商品修改");
        },
        onError: (error) => {
          // 409 REVISION_CONFLICT：别人先改了，停下来问人，绝不自动覆盖
          const found = revisionConflictOf(error);
          if (found) {
            pendingPatchRef.current = body;
            setConflict(found);
            return;
          }
          // 409 CONFLICT_BUSY：有任务在跑，这不是错误，只是「等一会儿」
          if (isConflictBusy(error)) {
            toast.warning(apiErrorMessage(error));
            return;
          }
          const message = apiErrorMessage(error);
          // 后端会拦「标题仍含违规词」；这时直接告诉用户点上面那个按钮
          if (/违规词|复核词/.test(message)) {
            setTitleError(message);
            toast.warning("标题未通过词库检查", {
              description: `${message}。点「按词库清理」后再保存。`,
            });
            return;
          }
          toast.error("保存失败", { description: message });
        },
      }
    );
  };

  const save = (): void => {
    if (!product) return;
    const trimmed = title.trim();
    const titleIssue = trimmed !== product.title ? validateTitle(trimmed) : null;
    if (titleIssue) {
      setTitleError(titleIssue);
      return;
    }
    setTitleError(null);

    const patchBody: { title?: string; groupId?: string | null } = {};
    if (trimmed !== product.title) patchBody.title = trimmed;
    if (groupId !== (product.groupId ?? "")) patchBody.groupId = groupId === "" ? null : groupId;
    if (Object.keys(patchBody).length === 0) return;

    submitPatch(patchBody, revisionRef.current);
  };

  const saveQualityReview = (): void => {
    if (!product) return;
    const prepared = buildQualityReviewRequest({
      weightKg: qualityWeight,
      packageLength: qualityLength,
      packageWidth: qualityWidth,
      packageHeight: qualityHeight,
      source: qualitySource,
      evidence: qualityEvidence,
    }, revisionRef.current ?? 0);
    if ("error" in prepared) {
      setQualityError(prepared.error);
      return;
    }
    setQualityError(null);
    qualityReviewMutation.mutate({
      id: product.id,
      review: prepared.request,
    }, {
      onSuccess: (result) => {
        revisionRef.current = result.revision;
        toast.success("商品重量与尺寸已核验并保存");
      },
      onError: (error) => {
        const conflictResult = revisionConflictOf(error);
        if (conflictResult) {
          toast.warning("商品已被其他人修改，请刷新详情后核对最新数据再提交");
          void detailQuery.refetch();
          return;
        }
        toast.error("核验信息保存失败", { description: apiErrorMessage(error) });
      },
    });
  };

  /** 「刷新看最新」：丢掉本地未提交的修改，重新读对方的版本 */
  const refreshAfterConflict = (): void => {
    setConflict(null);
    pendingPatchRef.current = null;
    // 重新拉详情：本地输入框由下面那个 useEffect 跟着 product 一起刷新
    void detailQuery.refetch();
    toast.info("已读取最新内容", {
      description: "你这次没保存的修改已丢弃，请重新确认后再改。",
    });
  };

  /** 「保留我的修改」：用 actual 作为新的 expectedRevision 覆盖提交（由人显式授权） */
  const keepMineAfterConflict = (): void => {
    const body = pendingPatchRef.current;
    const actual = conflict?.actual;
    if (!body) {
      setConflict(null);
      return;
    }
    if (actual === null || actual === undefined) {
      // 后端没给 actual 就没法安全地覆盖，只能去刷新
      setConflict(null);
      toast.error("后端没有返回当前版本号，无法安全覆盖，请刷新后再改");
      return;
    }
    // 先把基准版本抬到 actual，重试成功后 onSuccess 会再推进一次
    revisionRef.current = actual;
    submitPatch(body, actual);
    setConflict(null);
  };

  // 删除走确认弹窗：点一下就没了太危险（审查发现的 P1 问题）。
  // 底部垃圾桶图标和 footer 的「删除商品」按钮现在都走同一个确认流程。
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const deleteThis = (): void => {
    if (!product) return;
    setDeleteConfirmOpen(true);
  };
  const confirmDelete = (): void => {
    if (!product) return;
    remove.mutate(product.id, {
      onSuccess: () => {
        toast.success("已删除该商品（软删除，发布记录保留）");
        setDeleteConfirmOpen(false);
        onOpenChange(false);
      },
      onError: (error) => toast.error("删除失败", { description: apiErrorMessage(error) }),
    });
  };

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title={product?.title ? "商品详情" : "加载商品"}
      description={
        product
          ? `${product.detailId} · ${product.shopName || "未关联店铺"} · ${formatCount(product.skuCount)} 个 SKU`
          : productId ?? ""
      }
      footer={
        <>
          {canWrite && onRequestDelete && product && (
            <Button
              variant="ghost"
              icon={<Trash2 />}
              className="mr-auto text-danger-600 hover:bg-danger-50 hover:text-danger-700"
              onClick={() => onRequestDelete(product.id)}
            >
              删除商品
            </Button>
          )}
          <Button
            variant="secondary"
            onClick={() => onOpenChange(false)}
          >
            关闭
          </Button>
          {canWrite && (
            <Button
              variant="primary"
              icon={<Save />}
              disabled={!dirty}
              loading={patch.isPending}
              onClick={save}
            >
              保存修改
            </Button>
          )}
        </>
      }
    >
      {detailQuery.isLoading && <SkeletonRows rows={6} />}

      {detailQuery.isError && (
        <ErrorState
          title="加载商品详情失败"
          description={apiErrorMessage(detailQuery.error)}
          onRetry={() => void detailQuery.refetch()}
          minHeight={220}
        />
      )}

      {product && (
        <div className="flex flex-col gap-5">
          <div className="flex items-start gap-4">
            <div className="size-[120px] shrink-0 overflow-hidden rounded-md border border-border bg-canvas-inset">
              <RemoteImage src={product.image} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge meta={productStatus(product.status)} />
                {product.language && <Badge variant="info">{product.language}</Badge>}
                {product.site ? (
                  <Badge variant="neutral">{product.site}</Badge>
                ) : payloadSite ? (
                  <Badge variant="warning" title="商品站点字段为空，原始商品信息中记录了该站点">
                    {payloadSite} · 源数据
                  </Badge>
                ) : (
                  <Badge variant="warning">站点待核对</Badge>
                )}
              </div>
              <p className="mt-2 line-clamp-3 text-sm leading-5 text-fg">{product.title}</p>
              <p className="mt-1 font-mono text-2xs text-fg-muted">
                key {product.key} · 入库 {formatDate(product.savedAt)}
              </p>
              {/* 谁改的：后端没给 updatedBy 就整行不渲染，不显示「未知」占位 */}
              {product.updatedBy && (
                <p className="mt-1 flex flex-wrap items-center gap-1.5 text-2xs text-fg-muted">
                  <span className="inline-flex items-center gap-1">
                    <PencilLine aria-hidden className="size-3" strokeWidth={1.75} />
                    {product.updatedBy}
                    {product.updatedAt ? ` 编辑于 ${formatDate(product.updatedAt, "HH:mm")}` : " 编辑过"}
                  </span>
                  {typeof product.revision === "number" && (
                    <Badge variant="neutral" size="sm" title="每次保存成功后 +1，用于冲突检测">
                      v{product.revision}
                    </Badge>
                  )}
                </p>
              )}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 rounded-md border border-border-subtle bg-canvas-inset/50 p-3 sm:grid-cols-4">
            <div>
              <p className="text-2xs text-fg-muted">采购价</p>
              <p className="num mt-0.5 text-sm font-semibold text-fg">
                {formatPrice(product.purchasePriceMin, {
                  currency: product.purchaseCurrency,
                  withSymbol: false,
                })}
                {product.purchasePriceMin !== product.purchasePriceMax &&
                  ` – ${formatPrice(product.purchasePriceMax, {
                    currency: product.purchaseCurrency,
                    withSymbol: false,
                  })}`}
                <span className="ml-1 text-2xs font-normal text-fg-muted">
                  {product.purchaseCurrency}
                </span>
              </p>
            </div>
            <div>
              <p className="text-2xs text-fg-muted">SKU / 图片</p>
              <p className="num mt-0.5 text-sm font-semibold text-fg">
                {formatCount(product.skuCount)} / {formatCount(product.imageCount)}
              </p>
            </div>
            <div>
              <p className="text-2xs text-fg-muted">库存</p>
              <p
                className={cn(
                  "num mt-0.5 text-sm font-semibold",
                  product.stock > 0 && product.stock <= 10 ? "text-warning-600" : "text-fg"
                )}
              >
                {formatCount(product.stock)}
              </p>
            </div>
            <div>
              <p className="text-2xs text-fg-muted">近 30 天</p>
              <p className="num mt-0.5 text-sm font-semibold text-fg">
                {formatCount(product.salesLast30)}
              </p>
            </div>
          </div>

          <div className="grid gap-3 rounded-md border border-border-subtle bg-canvas-inset/50 p-3 sm:grid-cols-2">
            <div>
              <p className="text-2xs text-fg-muted">包裹重量</p>
              <p className="num mt-0.5 text-sm font-semibold text-fg">
                {packageWeight ? `${packageWeight} kg` : "缺少有效重量"}
              </p>
              <p className="mt-1 text-2xs text-fg-muted">
                {product.qualityReview
                  ? `已核验：${product.qualityReview.source} · ${product.qualityReview.verifiedByOperatorName} · ${formatDate(product.qualityReview.verifiedAt)}`
                  : payload?._sourceWeight
                    ? `记录的源重量：${String(payload._sourceWeight)} kg；尚未人工核验`
                    : "来源未记录，当前数值不能视为已核验"}
              </p>
            </div>
            <div>
              <p className="text-2xs text-fg-muted">包裹尺寸（长 × 宽 × 高）</p>
              <p className="num mt-0.5 text-sm font-semibold text-fg">
                {packageDimensions.map((value) => value ?? "缺失").join(" × ")}
                {dimensionsComplete ? " cm" : ""}
              </p>
              <p className="mt-1 text-2xs text-fg-muted">
                {product.qualityReview
                  ? `依据：${product.qualityReview.evidence} · 站点：${product.qualityReview.site || "未记录"}`
                  : dimensionsComplete && packageDimensions.join("×") === "30×20×10"
                  ? "数值为 30 × 20 × 10 cm；记录未标明来源，不能据此确认真实尺寸"
                  : "尺寸来源未记录；缺失或默认值应对照妙手或原始商品核验"}
              </p>
            </div>
          </div>

          {canManageQuality && (
            <section className="flex flex-col gap-3 rounded-md border border-border-subtle p-3" aria-label="重量与尺寸核验">
              <div>
                <SectionTitle>重量与尺寸核验</SectionTitle>
                <p className="mt-1 text-xs text-fg-muted">仅填写已从可靠来源核实的数据；保存后用于后续定价和发布。</p>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Field label="重量（kg）" required>
                  <Input id="quality-weight" type="number" min="0" step="0.001" value={qualityWeight} onChange={(event) => setQualityWeight(event.currentTarget.value)} />
                </Field>
                <Field label="长（cm）" required>
                  <Input id="quality-length" type="number" min="0" step="0.1" value={qualityLength} onChange={(event) => setQualityLength(event.currentTarget.value)} />
                </Field>
                <Field label="宽（cm）" required>
                  <Input id="quality-width" type="number" min="0" step="0.1" value={qualityWidth} onChange={(event) => setQualityWidth(event.currentTarget.value)} />
                </Field>
                <Field label="高（cm）" required>
                  <Input id="quality-height" type="number" min="0" step="0.1" value={qualityHeight} onChange={(event) => setQualityHeight(event.currentTarget.value)} />
                </Field>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="核验来源" required hint="例如妙手商品资料、原始商品页或实物测量">
                  <Input id="quality-source" maxLength={120} value={qualitySource} onChange={(event) => setQualitySource(event.currentTarget.value)} />
                </Field>
                <Field label="核验依据" required hint="记录可复查的信息，例如页面字段或测量记录">
                  <Input id="quality-evidence" maxLength={1000} value={qualityEvidence} onChange={(event) => setQualityEvidence(event.currentTarget.value)} />
                </Field>
              </div>
              {qualityError && <InlineError>{qualityError}</InlineError>}
              <div className="flex justify-end">
                <Button variant="primary" size="sm" icon={<Save />} loading={qualityReviewMutation.isPending} onClick={saveQualityReview}>
                  保存核验结果
                </Button>
              </div>
            </section>
          )}

          <div className="flex flex-col gap-3">
            <SectionTitle>标题</SectionTitle>
            {canWrite ? (
              <>
                <Field error={titleError} hint="25–255 字符（按字符计，中英文均计 1 个）">
                  <Textarea
                    value={title}
                    rows={3}
                    maxLength={255}
                    showCount
                    invalid={Boolean(titleError)}
                    onChange={(event) => {
                      setTitle(event.currentTarget.value);
                      setTitleError(null);
                    }}
                  />
                </Field>
                <div className="flex items-center gap-2">
                  <Button
                    variant="secondary"
                    size="sm"
                    icon={<Sparkles />}
                    loading={titlePreview.isPending}
                    onClick={cleanTitle}
                  >
                    按词库清理
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    icon={<CalendarClock />}
                    loading={titlePreview.isPending}
                    onClick={refreshYears}
                    title="把标题里 2024/2025 这类过时年份换成今年；型号与尺码不动"
                  >
                    刷新年份
                  </Button>
                  <span className="text-2xs text-fg-muted">
                    只把清理结果填回上面，确认后点「保存」；只改本地，不回写妙手
                  </span>
                </div>
              </>
            ) : (
              <p className="rounded-sm border border-border-subtle bg-canvas-inset/50 px-3 py-2 text-sm leading-5 text-fg">
                {product.title || "（无标题）"}
              </p>
            )}
          </div>

          <div className={cn("grid gap-3", canWrite && "sm:grid-cols-2")}>
            <Field label="平台类目" hint="cid 与类目名由拉取商品时写入，改类目请在仓库批量设置">
              <Input
                value={product.categoryName ?? "未分类"}
                disabled
                icon={<Tag />}
                readOnly
              />
            </Field>
            {canWrite ? (
              <Field label="自定义分组" hint="移出分组传 null；分组不影响平台类目">
                <Select
                  value={groupId}
                  onChange={setGroupId}
                  options={[
                    { value: "", label: "未分组" },
                    ...(groupsQuery.data?.groups ?? []).map((group) => ({
                      value: group.id,
                      label: `${group.name}（${group.count}）`,
                    })),
                  ]}
                />
              </Field>
            ) : product.group ? (
              <Field label="自定义分组">
                <Input value={product.group} disabled readOnly />
              </Field>
            ) : null}
          </div>

          <Separator />

          {/* 翻译检查：老版 coverage-heading 的等价物。
              逐字段列出仍有中文的路径——只看「翻译成功」会把漏翻字段当成已完成，
              上架后标题里还夹着中文。 */}
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-2">
              <SectionTitle>翻译检查</SectionTitle>
              {coverage && (
                <Badge variant={coverage.complete ? "brand" : "warning"} size="sm">
                  {coverage.complete ? "已翻译" : `${coverage.missingFields.length} 处待补`}
                </Badge>
              )}
            </div>
            {!coverage && (
              <p className="text-[13px] text-fg-muted">这件商品没有翻译检查记录。</p>
            )}
            {coverage && coverage.complete && (
              <p className="text-[13px] text-fg-muted">
                源文里的中文已全部翻译为
                {LANGUAGE_NAMES[coverage.targetLanguage] ?? coverage.targetLanguage}。
              </p>
            )}
            {/* 逐字段明细：老版 renderCoverage 的 coverage-row 列表。
                「保留」和「无需翻译」是正常的，「缺失」才是问题——
                只显示缺失项会让人以为其余都译了。 */}
            {coverage && coverage.requestedFields.length > 0 && (
              <details className="group">
                <summary className="cursor-pointer text-2xs text-fg-muted hover:text-fg">
                  逐字段明细（{coverage.requestedFields.length} 项）
                </summary>
                <ul className="mt-1 flex max-h-[180px] flex-col gap-0.5 overflow-y-auto">
                  {coverage.requestedFields.map(([path, status]) => {
                    const missing = coverage.missingFields.includes(path) || status === "missing";
                    const label = missing
                      ? "缺失"
                      : ({ translated: "已翻译", preserved: "保留",
                           not_required: "无需翻译", not_present: "未提供" }[
                           status
                         ] ?? status);
                    return (
                      <li
                        key={path}
                        className="flex items-baseline justify-between gap-2 rounded px-1 py-0.5 text-2xs"
                      >
                        <code className="min-w-0 flex-1 truncate text-fg-muted" title={path}>
                          {path || "$"}
                        </code>
                        <span
                          className={cn(
                            "shrink-0 font-medium",
                            missing ? "text-danger-700" : "text-fg-muted"
                          )}
                        >
                          {label}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </details>
            )}
            {coverage && !coverage.complete && (
              <>
                <p className="text-[13px] text-warning-700">
                  以下字段译文里仍有中文，发布前需要补齐：
                </p>
                <ul className="flex flex-col gap-1">
                  {coverage.missingFields.slice(0, 12).map((field) => (
                    <li
                      key={field}
                      className="truncate rounded border border-warning-600/20 bg-warning-50 px-2 py-1 font-mono text-2xs text-warning-700"
                      title={field}
                    >
                      {field}
                    </li>
                  ))}
                </ul>
                {coverage.missingFields.length > 12 && (
                  <p className="text-2xs text-fg-muted">
                    还有 {coverage.missingFields.length - 12} 处未列出
                  </p>
                )}
              </>
            )}
          </div>

          <Separator />

          <div className="flex flex-col gap-2">
            <SectionTitle>发布历史</SectionTitle>
            {data && data.publishHistory.length === 0 && (
              <p className="text-[13px] text-fg-muted">这件商品还没有发布记录。</p>
            )}
            <ul className="flex flex-col gap-1.5">
              {data?.publishHistory.map((entry) => {
                return (
                  <li
                    key={entry.logId}
                    className="flex items-center gap-2 rounded-md border border-border-subtle px-2.5 py-2"
                  >
                    <StatusBadge meta={publishStatus(entry.status)} />
                    <span className="min-w-0 flex-1 truncate text-[13px] text-fg">
                      {entry.shopName || entry.shopId}
                    </span>
                    <span className="num shrink-0 text-2xs text-fg-muted">
                      {formatDate(entry.requestedAt, "MM-DD HH:mm")}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>

          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <SectionTitle>图片翻译任务</SectionTitle>
              {/* 汇总：还有几条 ready 的任务没有公网地址 */}
              {fullJobs.data && <ImageJobUploadSummary jobs={fullJobs.data.items} />}
            </div>
            {/* 详情接口里的 imageJobs 是精简版（没有 publicUrl / uploadError），
                所以这里改取 GET /images/jobs?productId=… 的完整 job：
                没有完整字段就渲染不出「公网链接」和「上传失败原因」两列。
                完整列表还没回来时先用详情里的精简行占位，避免整块空白。
                空态只在两边都没有任务时显示 —— 提交翻译后详情那份是旧的，
                它说「没有任务」而完整列表里已经有新任务，两块会自相矛盾。 */}
            {fullJobs.data ? (
              fullJobs.data.items.length === 0 ? (
                <p className="text-[13px] text-fg-muted">还没有图片翻译任务。</p>
              ) : (
                <ul className="flex flex-col gap-1.5">
                  {fullJobs.data.items.map((job) => (
                    <ImageJobRow key={job.id} job={job} disabled={!canWrite} />
                  ))}
                </ul>
              )
            ) : data && data.imageJobs.length > 0 ? (
              <ul className="flex flex-col gap-1.5">
                {data.imageJobs.map((job) => (
                  <li
                    key={job.id}
                    className="flex items-center gap-2 rounded-md border border-border-subtle px-2.5 py-2"
                  >
                    <StatusBadge meta={imageStatus(job.status)} />
                    <span className="min-w-0 flex-1 truncate font-mono text-2xs text-fg-muted">
                      {job.source} · {job.id.slice(0, 10)}…
                    </span>
                    <span className="num shrink-0 text-2xs text-fg-muted">
                      {formatDate(job.createdAt, "MM-DD HH:mm")}
                    </span>
                  </li>
                ))}
              </ul>
            ) : data && !fullJobs.isError ? (
              <p className="text-[13px] text-fg-muted">还没有图片翻译任务。</p>
            ) : null}
            {fullJobs.isError && (
              <InlineError onRetry={() => void fullJobs.refetch()}>
                任务详情读取失败：{apiErrorMessage(fullJobs.error)}
              </InlineError>
            )}
            {/* 图片翻译页已经并入自动化上品，这里是死链；原地给出可用的检查入口 */}
            {canWrite && <div className="flex flex-wrap items-center gap-2">
              {/* 额度：老版顶栏的「图片翻译（本机 Key）· 今日 3/416」。
                  没有它，用户要等提交失败才知道额度用光了。 */}
              {usage.data && (
                <Tooltip
                  content={
                    usage.data.nextSlotSeconds > 0
                      ? `下一张约 ${Math.ceil(usage.data.nextSlotSeconds)} 秒后可提交`
                      : `本分钟 ${usage.data.usedThisMinute}/${usage.data.budgets.minute}`
                  }
                >
                  <Badge
                    variant={
                      usage.data.budget.remainingMilliyuan > 0 ? "neutral" : "danger"
                    }
                    size="sm"
                  >
                    今日 {usage.data.usedToday}/{usage.data.budgets.day}
                    {usage.data.budget.remainingMilliyuan > 0
                      ? ` · 余额 ${(usage.data.budget.remainingMilliyuan / 1000).toFixed(1)} 元`
                      : " · 预算已用尽"}
                  </Badge>
                </Tooltip>
              )}
              {/* 对账：重启前提交的任务没人认领，会永远挂在「处理中」 */}
              <Button
                variant="ghost"
                size="sm"
                icon={<RefreshCw />}
                loading={reconcileImages.isPending}
                onClick={() =>
                  reconcileImages.mutate(undefined, {
                    onSuccess: (result) => {
                      if (result.checked === 0) {
                        toast.info("没有待查询的图片任务");
                        return;
                      }
                      toast.success(
                        `已核对 ${result.checked} 个任务：完成 ${result.ready}、仍处理中 ${result.pending}${
                          result.failed ? `、失败 ${result.failed}` : ""
                        }`,
                        { description: "只更新状态，不会重新发起生成请求" }
                      );
                    },
                    onError: (error) => {
                      // 核对会写状态，撞上别人正在跑的任务就等一会儿，别报「失败」
                      if (isConflictBusy(error)) {
                        toast.warning(apiErrorMessage(error));
                        return;
                      }
                      toast.error("核对失败", { description: apiErrorMessage(error) });
                    },
                  })
                }
              >
                查询已提交任务
              </Button>
              <Button
                variant="secondary"
                size="sm"
                icon={<ScanSearch />}
                loading={analyze.isPending}
                disabled={mainImages.length === 0}
                onClick={runAnalyze}
              >
                识别主图文字
              </Button>
              {analyze.data && (
                <Button
                  variant="primary"
                  size="sm"
                  loading={translate.isPending}
                  onClick={translatePlanned}
                >
                  提交翻译
                </Button>
              )}
              {analyze.data && (
                <span className="text-2xs text-fg-muted">
                  {analyze.data.summary.withChinese > 0
                    ? `${analyze.data.summary.withChinese} 张有中文`
                    : "没发现中文"}
                  {analyze.data.summary.review > 0
                    ? ` · ${analyze.data.summary.review} 张需人工看`
                    : ""}
                </span>
              )}
            </div>}
            {canWrite && analyze.data && (
              <ul className="flex flex-col gap-1">
                {analyze.data.items.map((row) => (
                  <li
                    key={row.url}
                    className="flex items-start gap-2 rounded-sm bg-canvas-inset px-2 py-1.5 text-2xs"
                  >
                    <Badge
                      size="sm"
                      variant={
                        row.action === "translate"
                          ? "warning"
                          : row.action === "keep"
                            ? "success"
                            : "neutral"
                      }
                    >
                      {row.action === "translate" ? "译" : row.action === "keep" ? "留" : "看"}
                    </Badge>
                    <span className="shrink-0 text-fg-muted">#{row.index}</span>
                    <span className="min-w-0 flex-1 truncate text-fg-muted">{row.reason}</span>
                    <button
                      type="button"
                      className="shrink-0 rounded-xs px-1.5 py-0.5 text-2xs font-medium text-brand-700 hover:bg-brand-50"
                      onClick={() =>
                        setPlanOverride((prev) => ({
                          ...prev,
                          [row.url]: decisionOf(row) === "translate" ? "keep" : "translate",
                        }))
                      }
                    >
                      {decisionOf(row) === "translate" ? "改为保留" : "改为翻译"}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <Separator />

          {canWrite && <div className="flex items-center gap-2">
            <IconButton
              label="删除该商品"
              variant="danger"
              size="md"
              icon={<Trash2 />}
              disabled={remove.isPending}
              onClick={deleteThis}
            />
            <span className="text-2xs text-fg-muted">
              删除为软删除：图片结果文件会清理，发布记录保留。
            </span>
          </div>}

          {statusHint(product.status)}
        </div>
      )}

      {/* 删除确认：底部垃圾桶和 footer 删除按钮共用这一个弹窗 */}
      <ConfirmDialog
        open={deleteConfirmOpen}
        onOpenChange={setDeleteConfirmOpen}
        title={`删除「${product?.title ?? "该商品"}」？`}
        description="删除为软删除：图片结果文件会清理，发布记录保留。"
        confirmLabel="删除"
        danger
        loading={remove.isPending}
        onConfirm={confirmDelete}
      />

      {/* 并发冲突：别人先改了，停下来问人（绝不自动覆盖、绝不静默重试） */}
      <RevisionConflictDialog
        conflict={conflict}
        onOpenChange={(next) => {
          if (!next) setConflict(null);
        }}
        onRefresh={refreshAfterConflict}
        onKeepMine={keepMineAfterConflict}
        busy={patch.isPending}
      />
    </Drawer>
  );
}

function statusHint(status: string): React.ReactNode {
  if (status === "published") {
    return (
      <InlineError className="border-info-600/20 bg-info-50 text-info-700">
        已上架的商品改标题不会同步到平台，需要在妙手后台重新保存草稿。
      </InlineError>
    );
  }
  if (status === "archived") {
    return (
      <InlineError className="border-warning-600/20 bg-warning-50 text-warning-700">
        该商品已归档，仍可修复标题/类目后重新发布。
      </InlineError>
    );
  }
  return null;
}

export default ProductDetailDrawer;
