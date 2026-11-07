import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Images,
  PencilLine,
  Ruler,
  Save,
  Tag,
  Wallet,
  X,
} from "lucide-react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Input,
  SkeletonCardGrid,
  toast,
} from "@/components/ui";
import { PageContent, PageHeader } from "@/components/layout/PageHeader";
import { RemoteImage } from "@/features/shared/RemoteImage";
import { useProductDetail, usePatchProduct, useReviewProductQuality } from "@/lib/queries";
import { apiErrorMessage } from "@/lib/api";
import { useOperatorIdentity } from "@/lib/operator";
import { formatCount, formatDate, formatPrice } from "@/lib/utils";
import { cn } from "@/lib/utils";

/**
 * 商品详情**页面**（不是侧边抽屉）。
 *
 * 为什么改页面：抽屉里塞十几节内容，一屏放不下，看规格和价格要反复滚；
 * 而且抽屉会盖住列表，对照多件商品时非常别扭。改成独立页面后地址栏有
 * `/warehouse/:id`，可以直接分享、刷新、前进后退。
 *
 * 排版顺序按负责人 2026-11-07 的要求固定：
 *   ① 标题 ② 详情图（横向滑动，点开大图可翻页）
 *   ③ 类目 / ④ 尺码图 / ⑤ 包裹重量 / ⑥ 包裹尺寸 —— 这四项一行四块
 *   ⑦ SKU 颜色与尺码（颜色用大图卡片）⑧ 价格（框内滚动，列宽均分）
 *
 * 权限：学员只读（看得到全部内容，没有编辑控件）；
 *      老师/管理员可以改标题、类目、SKU、价格、尺码图、重量尺寸。
 */

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-[13px] font-semibold tracking-tight text-fg">{children}</h2>
  );
}

/** 从 payload 里取详情图 URL，去重保序。 */
function detailImageUrls(payload: Record<string, unknown> | null | undefined): string[] {
  const notes = String(payload?.notes ?? "");
  if (!notes) return [];
  const matches = notes.match(/https?:\/\/[^\s"'<>\\]+/gi) ?? [];
  return [...new Set(matches)];
}

/** SKU 按维度分组：颜色一组、尺码一组，保持原始顺序。 */
interface SkuDimension {
  name: string;
  values: Array<{ label: string; imageUrl?: string }>;
}

function skuDimensions(payload: Record<string, unknown> | null | undefined): SkuDimension[] {
  const props = payload?.skuPropertyList;
  if (!Array.isArray(props)) return [];
  return props
    .map((prop) => {
      const record = (prop ?? {}) as Record<string, unknown>;
      const name = String(record.attrName ?? record.propName ?? "").trim();
      const values = Array.isArray(record.attrValueList) ? record.attrValueList : [];
      return {
        name: name || "规格",
        values: values
          .map((value) => {
            const item = (value ?? {}) as Record<string, unknown>;
            const label = String(item.attrValue ?? "").trim();
            if (!label) return null;
            const imageUrl = String(
              item.imgUrl ?? item.imageUrl ?? (Array.isArray(item.imgUrls) ? item.imgUrls[0] : "") ?? ""
            );
            return imageUrl ? { label, imageUrl } : { label };
          })
          .filter((item): item is { label: string; imageUrl?: string } => item !== null),
      };
    })
    .filter((dimension) => dimension.values.length > 0);
}

/** 各 SKU 的价格与库存（用于第 ⑤ 节）。 */
interface SkuPriceRow {
  name: string;
  price: string;
  purchasePrice: string;
  stock: string;
}

function skuPrices(payload: Record<string, unknown> | null | undefined): SkuPriceRow[] {
  const map = payload?.skuMap;
  if (!map || typeof map !== "object") return [];
  const dimensions = skuDimensions(payload);
  // skuMap 的 key 是 `;值1;值2;`，段顺序与维度顺序一致
  const idToLabel = new Map<string, string>();
  for (const dimension of dimensions) {
    // attrValueId 不在 values 里（上面只留了 label），这里从原始 payload 补
    void dimension;
  }
  const props = Array.isArray(payload?.skuPropertyList) ? payload!.skuPropertyList : [];
  for (const prop of props) {
    const record = (prop ?? {}) as Record<string, unknown>;
    const values = Array.isArray(record.attrValueList) ? record.attrValueList : [];
    for (const value of values) {
      const item = (value ?? {}) as Record<string, unknown>;
      const id = String(item.attrValueId ?? "").trim();
      const label = String(item.attrValue ?? "").trim();
      if (id && label) idToLabel.set(id, label);
    }
  }
  const rows: SkuPriceRow[] = [];
  for (const [key, value] of Object.entries(map as Record<string, unknown>)) {
    const sku = (value ?? {}) as Record<string, unknown>;
    if (String(sku.isDelete ?? "0") === "1") continue;
    const parts = String(key)
      .split(";")
      .map((part) => part.trim())
      .filter(Boolean);
    const name = parts.map((part) => idToLabel.get(part) ?? part).join(" / ");
    rows.push({
      name: name || key,
      price: String(sku.price ?? ""),
      purchasePrice: String(sku.originPrice ?? ""),
      stock: String(sku.stock ?? sku.quantity ?? "0"),
    });
  }
  return rows;
}

export function ProductDetailPage() {
  const { ident } = useParams<{ ident: string }>();
  const navigate = useNavigate();
  const identity = useOperatorIdentity();
  const isMember = identity?.role === "member";

  // includePayload=true：详情页要展示详情图、SKU 规格、价格、尺码图，
  // 这些都在 payload 里（列表接口不带 payload，体积太大）。
  const detail = useProductDetail(ident ?? null, true);
  const update = usePatchProduct();
  const reviewQuality = useReviewProductQuality();

  const product = detail.data?.product;
  const payload = (product?.payload ?? null) as Record<string, unknown> | null;

  const [title, setTitle] = useState("");
  const [titleDirty, setTitleDirty] = useState(false);
  const [lightbox, setLightbox] = useState<number | null>(null);

  const [weight, setWeight] = useState("");
  const [length, setLength] = useState("");
  const [width, setWidth] = useState("");
  const [height, setHeight] = useState("");
  const [qualitySource, setQualitySource] = useState("");
  const [qualityEvidence, setQualityEvidence] = useState("");

  useEffect(() => {
    if (!product) return;
    setTitle(product.title ?? "");
    setTitleDirty(false);
    // 依赖故意只写 product.id：同一件商品保存后 refetch 会把 product 换成
    // 新对象，如果依赖整个 product，用户正在编辑的标题会被冲掉。
    // 只在切换到另一件商品时才重置。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [product?.id]);

  useEffect(() => {
    if (!payload) return;
    setWeight(String(payload.weight ?? ""));
    setLength(String(payload.packageLength ?? ""));
    setWidth(String(payload.packageWidth ?? ""));
    setHeight(String(payload.packageHeight ?? ""));
  }, [product?.id, payload]);

  const images = useMemo(() => detailImageUrls(payload), [payload]);
  const dimensions = useMemo(() => skuDimensions(payload), [payload]);
  const prices = useMemo(() => skuPrices(payload), [payload]);

  const mainImage = product?.image ?? "";
  const allImages = useMemo(
    () => (mainImage ? [mainImage, ...images.filter((url) => url !== mainImage)] : images),
    [mainImage, images]
  );

  const saveTitle = (): void => {
    if (!product) return;
    update.mutate(
      { id: String(product.id), patch: { title } },
      {
        onSuccess: () => {
          setTitleDirty(false);
          toast.success("标题已保存");
          void detail.refetch();
        },
        onError: (error) => toast.error("保存标题失败", { description: apiErrorMessage(error) }),
      }
    );
  };

  const saveQuality = (): void => {
    if (!product) return;
    reviewQuality.mutate(
      {
        id: String(product.id),
        review: {
          weightKg: Number(weight),
          packageLength: Number(length),
          packageWidth: Number(width),
          packageHeight: Number(height),
          source: qualitySource,
          evidence: qualityEvidence,
          // 版本号用于并发冲突检测：详情页已加载的 revision 就是用户看到的那一版
          expectedRevision: product.revision ?? 0,
        },
      },
      {
        onSuccess: () => {
          toast.success("重量与尺寸已保存");
          void detail.refetch();
        },
        onError: (error) => toast.error("保存失败", { description: apiErrorMessage(error) }),
      }
    );
  };

  const closeLightbox = (): void => setLightbox(null);
  const stepLightbox = useCallback((delta: number): void => {
    setLightbox((current) => {
      if (current === null) return current;
      const total = allImages.length;
      if (total === 0) return current;
      return (current + delta + total) % total;
    });
  }, [allImages.length]);

  // 大图模式下支持键盘左右翻页 / Esc 关闭
  useEffect(() => {
    if (lightbox === null) return;
    const handler = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setLightbox(null);
      if (event.key === "ArrowLeft") stepLightbox(-1);
      if (event.key === "ArrowRight") stepLightbox(1);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [lightbox, stepLightbox]);

  if (detail.isLoading) {
    return (
      <>
        <PageHeader title="商品详情" actions={<Button variant="ghost" icon={<ArrowLeft />} onClick={() => navigate("/warehouse")}>返回</Button>} />
        <PageContent width="full" padding="p-4">
          <Card padding="normal"><SkeletonCardGrid count={1} /></Card>
        </PageContent>
      </>
    );
  }

  if (detail.isError || !product) {
    return (
      <>
        <PageHeader title="商品详情" actions={<Button variant="ghost" icon={<ArrowLeft />} onClick={() => navigate("/warehouse")}>返回</Button>} />
        <PageContent width="full" padding="p-4">
          <Card padding="normal">
            <EmptyState
              title="没有找到这件商品"
              description={detail.isError ? apiErrorMessage(detail.error) : "商品可能已被删除"}
              action={{ label: "返回成品仓库", onClick: () => navigate("/warehouse") }}
            />
          </Card>
        </PageContent>
      </>
    );
  }

  const readOnlyHint = isMember ? "学员只读，需要修改请联系老师" : null;

  return (
    <>
      <PageHeader
        title="商品详情"
        description={`${product.detailId} · ${product.shopName || "未关联店铺"} · ${formatCount(product.skuCount)} 个 SKU`}
        actions={
          <span className="flex items-center gap-2">
            {readOnlyHint && <span className="text-xs text-fg-muted">{readOnlyHint}</span>}
            <Button variant="ghost" icon={<ArrowLeft />} onClick={() => navigate("/warehouse")}>
              返回
            </Button>
            {!isMember && (
              <Button
                variant="primary"
                icon={<Save />}
                disabled={!titleDirty}
                loading={update.isPending}
                onClick={saveTitle}
              >
                保存修改
              </Button>
            )}
          </span>
        }
      />

      <PageContent width="full" padding="p-4" scroll>
        <div className="flex flex-col gap-4">
          {/* ① 标题 */}
          <Card padding="normal">
            <div className="flex flex-wrap items-center gap-2">
              <SectionTitle>① 标题</SectionTitle>
              {product.language && <Badge variant="info" size="sm">{product.language}</Badge>}
              {product.site && <Badge variant="neutral" size="sm">{product.site}</Badge>}
              {product.needsRecollect && (
                <Badge variant="warning" size="sm" title={product.needsRecollectReason}>
                  需重新采集
                </Badge>
              )}
              {product.updatedBy && (
                <span className="inline-flex items-center gap-1 text-2xs text-fg-muted">
                  <PencilLine className="size-3" aria-hidden strokeWidth={1.75} />
                  {product.updatedBy}
                  {product.updatedAt ? ` 编辑于 ${formatDate(product.updatedAt, "HH:mm")}` : ""}
                </span>
              )}
            </div>
            {isMember ? (
              <p className="mt-3 text-sm leading-6 text-fg">{product.title || "（无标题）"}</p>
            ) : (
              <div className="mt-3">
                <Field hint="25–255 字符；改完点右上角「保存修改」">
                  <Input
                    value={title}
                    maxLength={255}
                    onChange={(event) => {
                      setTitle(event.currentTarget.value);
                      setTitleDirty(true);
                    }}
                  />
                </Field>
              </div>
            )}
          </Card>

          {/* ② 详情图（可点开大图） */}
          <Card padding="normal">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="flex items-center gap-2">
                <SectionTitle>② 详情图</SectionTitle>
                <Badge variant="neutral" size="sm">
                  <Images className="mr-1 size-3" aria-hidden /> {formatCount(allImages.length)} 张
                </Badge>
              </span>
              <span className="text-2xs text-fg-muted">点击任意图片查看大图，可用 ← → 翻页</span>
            </div>
            {allImages.length === 0 ? (
              <p className="mt-3 text-xs text-fg-muted">这件商品没有详情图</p>
            ) : (
              // 横向滑动而不是换行铺满：详情图动辄十几张，铺满会把后面的
              // 类目/规格/价格全部推到很下面，用户得滚很久才能看到关键信息。
              // 单行横向滚动保持这一节高度固定，鼠标滚轮/触控板左右滑都能翻。
              <div className="mt-3 -mx-1 flex gap-2 overflow-x-auto px-1 pb-2">
                {allImages.map((url, index) => (
                  <button
                    key={`${url}-${index}`}
                    type="button"
                    onClick={() => setLightbox(index)}
                    className={cn(
                      "group relative aspect-square w-[132px] shrink-0 overflow-hidden rounded-sm border border-border bg-canvas-inset",
                      "transition-shadow hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/40"
                    )}
                    title={`第 ${index + 1} 张`}
                  >
                    <RemoteImage src={url} className="size-full object-cover" />
                    {index === 0 && (
                      <span className="absolute left-1 top-1 rounded-xs bg-brand-600/90 px-1 py-0.5 text-[10px] font-medium text-white">
                        主图
                      </span>
                    )}
                  </button>
                ))}
              </div>
            )}
          </Card>

          {/* ③④⑤ 一行四块：类目 / 尺码图 / 重量 / 尺寸
              这几项都是短字段，各占一张整卡会把页面拉得很长，
              合并成一行四列后一屏就能看全。 */}
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {/* ③ 类目 */}
            <Card padding="normal">
              <SectionTitle>③ 类目</SectionTitle>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <span className="inline-flex items-center gap-1.5 rounded-sm bg-canvas-inset px-2 py-1 text-sm text-fg">
                  <Tag className="size-3.5 text-fg-muted" aria-hidden strokeWidth={1.75} />
                  {product.categoryName || "未设置"}
                </span>
              </div>
              {product.cid && (
                <p className="mt-2 font-mono text-2xs text-fg-muted">cid {product.cid}</p>
              )}
              <p className="mt-2 text-2xs leading-4 text-fg-muted">
                {isMember ? "类目由老师设置" : "改类目请用仓库「批量改类目」"}
              </p>
            </Card>

            {/* ④ 尺码图（与类目/重量/尺寸同一行） */}
            <Card padding="normal">
              <div className="flex items-center justify-between gap-2">
                <SectionTitle>④ 尺码图</SectionTitle>
                <Ruler className="size-3.5 text-fg-muted" aria-hidden strokeWidth={1.75} />
              </div>
              {(() => {
                const chart = String(payload?.sizeChart ?? "");
                if (!chart) {
                  return <p className="mt-3 text-xs text-fg-muted">未上传尺码图</p>;
                }
                return (
                  <button
                    type="button"
                    onClick={() => {
                      const index = allImages.indexOf(chart);
                      if (index >= 0) setLightbox(index);
                      else window.open(chart, "_blank", "noopener,noreferrer");
                    }}
                    className="mt-3 block h-24 w-full overflow-hidden rounded-sm border border-border bg-canvas-inset"
                    title="点击查看大图"
                  >
                    <RemoteImage src={chart} className="size-full object-contain" />
                  </button>
                );
              })()}
            </Card>

            {/* ⑦-1 重量 */}
            <Card padding="normal">
              <div className="flex items-center justify-between gap-2">
                <SectionTitle>⑤ 包裹重量</SectionTitle>
                <Badge
                  variant={product.qualityReview ? "success" : "warning"}
                  size="sm"
                  title={product.qualityReview
                    ? `核验人：${product.qualityReview.verifiedByOperatorName}`
                    : "当前规格来源未确认，需对照妙手资料或实物核实"}
                >
                  {product.qualityReview ? "已核验" : "待核验"}
                </Badge>
              </div>
              <p className="num mt-3 text-lg font-semibold text-fg">
                {payload?.weight ? `${String(payload.weight)} kg` : "—"}
              </p>
              <p className="mt-1 text-2xs text-fg-muted">
                {product.qualityReview
                  ? `${product.qualityReview.source} · ${product.qualityReview.verifiedByOperatorName}`
                  : "来源未记录"}
              </p>
            </Card>

            {/* ⑦-2 尺寸 */}
            <Card padding="normal">
              <SectionTitle>⑥ 包裹尺寸</SectionTitle>
              <p className="num mt-3 text-lg font-semibold text-fg">
                {[payload?.packageLength, payload?.packageWidth, payload?.packageHeight]
                  .map((value) => (value === undefined || value === null || value === "" ? "—" : String(value)))
                  .join(" × ")}
                <span className="ml-1 text-xs font-normal text-fg-muted">cm</span>
              </p>
              <p className="mt-1 text-2xs text-fg-muted">长 × 宽 × 高</p>
            </Card>
          </div>

          {/* ④ SKU 颜色 / 尺码 */}
          <Card padding="normal">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <SectionTitle>⑦ 规格（颜色 / 尺码）</SectionTitle>
              <Badge variant="neutral" size="sm">{formatCount(product.skuCount)} 个 SKU</Badge>
            </div>
            {dimensions.length === 0 ? (
              <p className="mt-3 text-xs text-fg-muted">这件商品没有规格信息</p>
            ) : (
              <div className="mt-3 flex flex-col gap-4">
                {dimensions.map((dimension) => {
                  // 有图的维度（颜色）用大图卡片，无图的（尺码）用小胶囊
                  const hasImages = dimension.values.some((value) => value.imageUrl);
                  return (
                    <div key={dimension.name}>
                      <p className="text-2xs font-medium text-fg-secondary">{dimension.name}</p>
                      {hasImages ? (
                        <div className="mt-2 flex flex-wrap gap-3">
                          {dimension.values.map((value) => (
                            <div
                              key={value.label}
                              className="flex w-20 flex-col items-center gap-1.5"
                            >
                              <span className="size-20 overflow-hidden rounded-sm border border-border bg-canvas-inset">
                                {value.imageUrl ? (
                                  <RemoteImage src={value.imageUrl} className="size-full object-cover" />
                                ) : null}
                              </span>
                              <span className="line-clamp-2 text-center text-2xs leading-4 text-fg">
                                {value.label}
                              </span>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="mt-2 flex flex-wrap gap-2">
                          {dimension.values.map((value) => (
                            <span
                              key={value.label}
                              className="inline-flex min-w-[40px] items-center justify-center rounded-sm border border-border bg-surface px-2 py-1 text-xs text-fg"
                            >
                              {value.label}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>

          {/* ⑤ 价格 */}
          <Card padding="normal">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <SectionTitle>⑧ 价格</SectionTitle>
              <span className="inline-flex items-center gap-1 text-2xs text-fg-muted">
                <Wallet className="size-3" aria-hidden strokeWidth={1.75} />
                采购价 {product.purchaseCurrency || "CNY"}
              </span>
            </div>
            {/* 固定高度 + 内部纵向滚动：一个 24 SKU 的商品能列出 24 行，
                全部铺开会把重量尺寸挤到屏幕外。给个框在里面滚，页面上
                其它信息的位置就稳定了。列宽用 table-fixed + 百分比均分，
                不同商品 SKU 数量不同也不会左右跳动。 */}
            <div className="mt-3 max-h-[320px] overflow-y-auto rounded-md border border-border-subtle">
              {prices.length === 0 ? (
                <p className="p-3 text-xs text-fg-muted">没有可展示的 SKU 价格</p>
              ) : (
                <table className="w-full table-fixed text-sm">
                  <colgroup>
                    <col className="w-[40%]" />
                    <col className="w-[20%]" />
                    <col className="w-[20%]" />
                    <col className="w-[20%]" />
                  </colgroup>
                  <thead className="sticky top-0 bg-canvas-inset">
                    <tr className="border-b border-border-subtle text-left text-2xs text-fg-muted">
                      <th className="py-2 pl-3 pr-2 font-medium">规格</th>
                      <th className="py-2 pr-2 text-right font-medium">售价</th>
                      <th className="py-2 pr-2 text-right font-medium">采购价</th>
                      <th className="py-2 pr-3 text-right font-medium">库存</th>
                    </tr>
                  </thead>
                  <tbody>
                    {prices.map((row) => (
                      <tr key={row.name} className="border-b border-border-subtle/60 last:border-0">
                        <td className="truncate py-2 pl-3 pr-2 text-fg" title={row.name}>{row.name}</td>
                        <td className="num truncate py-2 pr-2 text-right font-medium text-fg">{row.price || "—"}</td>
                        <td className="num truncate py-2 pr-2 text-right text-fg-secondary">{row.purchasePrice || "—"}</td>
                        <td className="num truncate py-2 pr-3 text-right text-fg-secondary">{row.stock}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-4 border-t border-border-subtle pt-3 text-xs">
              <span className="text-fg-muted">
                采购价区间：
                <span className="num ml-1 font-medium text-fg">
                  {formatPrice(product.purchasePriceMin, {
                    currency: product.purchaseCurrency,
                    withSymbol: false,
                  })}
                  {product.purchasePriceMin !== product.purchasePriceMax &&
                    ` – ${formatPrice(product.purchasePriceMax, {
                      currency: product.purchaseCurrency,
                      withSymbol: false,
                    })}`}
                </span>
              </span>
            </div>
          </Card>

          {/* 老师/管理员可编辑；学员只读（数值见上方 ⑤⑥ 两张卡片） */}
          {!isMember && (
            <Card padding="normal">
              <SectionTitle>修改重量与尺寸</SectionTitle>
              <p className="mt-1 text-2xs text-fg-muted">
                填写已从可靠来源核实的数据；保存后用于定价和发布。
              </p>
              <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Field label="重量（kg）" required>
                  <Input type="number" min="0" step="0.001" value={weight} onChange={(event) => setWeight(event.currentTarget.value)} />
                </Field>
                <Field label="长（cm）" required>
                  <Input type="number" min="0" step="0.1" value={length} onChange={(event) => setLength(event.currentTarget.value)} />
                </Field>
                <Field label="宽（cm）" required>
                  <Input type="number" min="0" step="0.1" value={width} onChange={(event) => setWidth(event.currentTarget.value)} />
                </Field>
                <Field label="高（cm）" required>
                  <Input type="number" min="0" step="0.1" value={height} onChange={(event) => setHeight(event.currentTarget.value)} />
                </Field>
              </div>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <Field label="核验来源" required hint="例如妙手商品资料、原始商品页或实物测量">
                  <Input maxLength={120} value={qualitySource} onChange={(event) => setQualitySource(event.currentTarget.value)} />
                </Field>
                <Field label="核验依据" required hint="记录可复查的信息">
                  <Input maxLength={1000} value={qualityEvidence} onChange={(event) => setQualityEvidence(event.currentTarget.value)} />
                </Field>
              </div>
              <div className="mt-3 flex justify-end">
                <Button
                  variant="primary"
                  size="sm"
                  icon={<Save />}
                  loading={reviewQuality.isPending}
                  onClick={saveQuality}
                >
                  保存重量与尺寸
                </Button>
              </div>
            </Card>
          )}
        </div>
      </PageContent>

      {/* 大图查看器：点击详情图后打开，左右翻页、Esc 关闭 */}
      {lightbox !== null && allImages[lightbox] && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="查看大图"
          className="fixed inset-0 z-50 flex items-center justify-center bg-inverse/85 p-4 backdrop-blur-sm"
          onClick={closeLightbox}
        >
          <button
            type="button"
            className="absolute right-4 top-4 inline-flex size-9 items-center justify-center rounded-full bg-white/10 text-white hover:bg-white/20"
            onClick={closeLightbox}
            aria-label="关闭"
          >
            <X className="size-5" />
          </button>
          {allImages.length > 1 && (
            <>
              <button
                type="button"
                className="absolute left-4 inline-flex size-10 items-center justify-center rounded-full bg-white/10 text-white hover:bg-white/20"
                onClick={(event) => {
                  event.stopPropagation();
                  stepLightbox(-1);
                }}
                aria-label="上一张"
              >
                <ChevronLeft className="size-6" />
              </button>
              <button
                type="button"
                className="absolute right-4 inline-flex size-10 items-center justify-center rounded-full bg-white/10 text-white hover:bg-white/20"
                onClick={(event) => {
                  event.stopPropagation();
                  stepLightbox(1);
                }}
                aria-label="下一张"
              >
                <ChevronRight className="size-6" />
              </button>
            </>
          )}
          <div
            className="max-h-[88vh] max-w-[92vw] overflow-hidden"
            onClick={(event) => event.stopPropagation()}
          >
            <img
              src={allImages[lightbox]}
              alt={`第 ${lightbox + 1} 张`}
              referrerPolicy="no-referrer"
              className="max-h-[88vh] max-w-[92vw] rounded-md object-contain"
            />
            <p className="mt-2 text-center text-xs text-white/70">
              {lightbox + 1} / {allImages.length}
            </p>
          </div>
        </div>
      )}
    </>
  );
}
