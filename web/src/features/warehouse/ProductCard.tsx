// features/warehouse —— 商品卡（design-system §5.7 商品卡规格）
// 图片 1:1 懒加载、标题恒 2 行、价格等宽数字。卡片信息按负责人要求精简：
//   · 左上角只显示「认领状态」（未认领 / 已认领-绿色），不再显示「待上架」；
//   · 重量显示在图片右下角图片数量之上；
//   · 类目与 SKU 数移到价格最右侧；
//   · 去掉「近 30 天销量」与「规格已核验」。

import { Images, Package } from "lucide-react";
import { cn, formatCount, formatPrice } from "@/lib/utils";
import type { Product } from "@/lib/types";
import { RemoteImage } from "@/features/shared/RemoteImage";
import { Checkbox } from "@/components/ui";

export interface ProductCardProps {
  product: Product;
  isMember?: boolean;
  selected: boolean;
  onToggle: (id: string) => void;
  onOpen?: (product: Product) => void;
  index: number;
  /** 已废弃：卡片不再显示「谁编辑于何时」（负责人 2026-11-11 要求去掉）。
   *  保留参数只为不破坏既有调用方，传什么都不影响渲染。 */
  showEditor?: boolean;
}

function priceText(product: Product): { symbol: string; amount: string; range: boolean } | null {
  const min = product.purchasePriceMin;
  const max = product.purchasePriceMax;
  if ((min === null || min === undefined || min === "") && (max === null || max === "")) return null;
  const currency = product.purchaseCurrency || "CNY";
  const single = formatPrice(min, { currency, withSymbol: false });
  const other = formatPrice(max, { currency, withSymbol: false });
  const isRange = single !== other;
  const symbolText = formatPrice(0, { currency, decimals: 0 }).replace(/[\d.,]/g, "");
  return {
    symbol: symbolText,
    amount: isRange ? `${single} – ${other}` : single,
    range: isRange,
  };
}

/** 重量文案：payload 里是 kg，显示成「0.6kg」。没有重量就不占位。 */
function weightText(product: Product): string {
  const value = typeof product.weight === "number" ? product.weight : Number(product.weight);
  if (!Number.isFinite(value) || value <= 0) return "";
  return `${value.toFixed(2).replace(/\.?0+$/, "")}kg`;
}

/**
 * 标题兜底：上游采集有时只给了占位标题（单个字母 / 纯符号 / 空白），
 * 或者商品在上游已被删除、拿不到真标题。这种情况下与其显示「Item 123」，
 * 不如明确告诉运营「这件商品没有标题」，一眼就知道该怎么处理（删掉或重新采集）。
 * 不写中文——商品语言是马来语/英语时混进中文很难看。
 */
function displayTitle(product: Product): string {
  const raw = (product.title || "").trim();
  const meaningful = raw.length >= 4 && /[\u4e00-\u9fa5A-Za-z]{2,}/.test(raw);
  if (meaningful) return raw;
  return "（无标题，请重新采集）";
}

/** 标题是否为占位/缺失状态：true 时卡片用弱化样式，不抢正常商品注意力。 */
function isMissingTitle(product: Product): boolean {
  const raw = (product.title || "").trim();
  return !(raw.length >= 4 && /[\u4e00-\u9fa5A-Za-z]{2,}/.test(raw));
}

export function ProductCard({
  product,
  isMember = false,
  selected,
  onToggle,
  onOpen,
  index,
}: ProductCardProps) {
  const price = priceText(product);
  const weight = weightText(product);

  return (
    <article
      data-selected={selected ? "true" : undefined}
      className={cn(
        "group relative flex flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-1",
        "transition-[transform,box-shadow,border-color] duration-card ease-out",
        "hover:-translate-y-0.5 hover:border-border-strong hover:shadow-2",
        "focus-within:ring-2 focus-within:ring-brand-600/25",
        selected && "border-brand-600 ring-1 ring-brand-600"
      )}
    >
      {/* 图片区：白图 + 白底正文容易糊在一起。用「内嵌槽 + 图片自身投影」区分：
          外层 canvas-inset 做浅槽，图片本身带 ring + 阴影浮在槽里，
          这样即使图片是纯白也能看出边界，正文仍是干净的白。 */}
      <div className="relative aspect-square w-full overflow-hidden bg-canvas-inset p-1.5">
        <div className="relative h-full w-full overflow-hidden rounded-md ring-1 ring-black/[0.06] shadow-[0_1px_3px_rgba(15,23,42,0.10)]">
          <RemoteImage
            src={product.image}
            eager={index < 8}
            zoomOnHover
            className="[&_svg]:size-8"
          />
        </div>

        {/* 左上角状态：已发布（蓝）> 已认领（绿）> 未认领（灰）。
            发布过就不该再显示「已认领」——那是两个不同阶段，
            混在一起会让人以为商品还在采集箱里没上架。 */}
        <span
          className={cn(
            "absolute left-2 top-2 inline-flex items-center gap-1 rounded-xs px-1.5 py-0.5 text-[10px] font-medium leading-3 backdrop-blur-[2px]",
            product.publishedShopIds && product.publishedShopIds.length > 0
              ? "bg-info-600/92 text-white"
              : product.claimed
                ? "bg-success-600/92 text-white"
                : "bg-white/92 text-fg-muted"
          )}
          title={
            product.publishedShopIds && product.publishedShopIds.length > 0
              ? `已发布到店铺 ${product.publishedShopIds.join("、")}`
              : product.claimed
                ? (product.claimedShopId
                  ? `已认领到店铺 ${product.claimedShopId} 的采集箱`
                  : "已认领到当前账号的采集箱")
                : (isMember ? "未认领：尚未采集到你的采集箱" : "未认领：尚未采集到采集箱")
          }
        >
          <span
            className={cn(
              "size-1.5 rounded-full",
              (product.publishedShopIds && product.publishedShopIds.length > 0)
                || product.claimed ? "bg-white" : "bg-fg-subtle"
            )}
          />
          {product.publishedShopIds && product.publishedShopIds.length > 0
            ? "已发布"
            : product.claimed ? "已认领" : "未认领"}
        </span>

        {/* 需重新采集标记：product_standard 判定数据缺失（标题占位、无主图、
            缺类目、无 SKU）时打这个标。这类商品认领时会被静默跳过，
            不标出来操作者根本不知道为什么这件一直没上架。 */}
        {product.needsRecollect && (
          <span
            className="absolute left-2 top-7 inline-flex items-center gap-1 rounded-xs bg-warning-600/92 px-1.5 py-0.5 text-[10px] font-medium leading-3 text-white backdrop-blur-[2px]"
            title={product.needsRecollectReason || "数据缺失，需要重新采集"}
          >
            需重采
          </span>
        )}

        <div
          className={cn(
            "absolute right-2 top-2",
            "transition-opacity duration-fast ease-in-out",
            selected
              ? "opacity-100"
              : "opacity-60 group-hover:opacity-100 group-focus-within:opacity-100"
          )}
        >
          <Checkbox
            checked={selected}
            onChange={() => onToggle(product.id)}
            aria-label={`选择 ${product.title || product.detailId}`}
          />
        </div>

        {/* 右下角：重量在图片数量之上 */}
        <div className="absolute bottom-2 right-2 flex flex-col items-end gap-1">
          {weight && (
            <span
              className="inline-flex items-center rounded-xs bg-inverse/55 px-1.5 py-0.5 text-2xs text-white"
              title="包裹重量"
            >
              <span className="num">{weight}</span>
            </span>
          )}
          {product.imageCount > 1 && (
            <span className="inline-flex items-center gap-1 rounded-xs bg-inverse/55 px-1.5 py-0.5 text-2xs text-white">
              <Images className="size-3" strokeWidth={2} />
              <span className="num">{product.imageCount}</span>
            </span>
          )}
        </div>
      </div>

      <button
        type="button"
        onClick={() => onOpen?.(product)}
        className={cn(
          "flex flex-1 flex-col gap-2 p-3 text-left",
          // 正文保持白底（灰底视觉上偏脏，负责人否掉）。图片与正文的分界交给
          // 图片自身的内阴影/边缘 + 卡片描边，而不是给正文上灰。
          "bg-surface",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-600/32"
        )}
      >
        <h3
          className={cn(
            "line-clamp-2 min-h-[40px] text-sm font-medium leading-5",
            isMissingTitle(product) ? "text-fg-muted italic" : "text-fg"
          )}
          title={product.title || undefined}
        >
          {displayTitle(product)}
        </h3>

        {/* 价格靠左，类目 + SKU 数贴最右：用 justify-between 撑开整行。
            实测 ml-auto 在这个工程里没生成出 margin-left:auto（算出 0px），
            所以不能靠 auto margin 靠右。 */}
        <div className="flex min-h-[24px] w-full items-baseline justify-between gap-2">
          {price ? (
            <span className="num shrink-0 text-h3 font-semibold text-fg">
              <span className="mr-0.5 text-xs font-medium text-fg-secondary">{price.symbol}</span>
              {price.amount}
            </span>
          ) : (
            <span className="shrink-0 text-sm text-fg-muted">价格未录入</span>
          )}
          <span className="flex shrink-0 items-center gap-2 text-2xs text-fg-muted">
            {product.categoryName && (
              <span className="max-w-[80px] truncate" title={product.categoryName}>
                {product.categoryName}
              </span>
            )}
            <span className="flex shrink-0 items-center gap-0.5" title="SKU 数">
              <Package className="size-3" strokeWidth={1.75} />
              <span className="num">{formatCount(product.skuCount)}</span>
            </span>
          </span>
        </div>

        {/* 分组标签已按负责人要求去掉（2026-11-12）：
            卡片下方再重复一次分组名（「运动鞋」「精品」…）没有意义 ——
            分组已经在顶部筛选栏里，而且同一个分组下每张卡都显示同样两个字，
            只是占高度。想看归在哪个分组，看顶部选中的 Chip 就行。
            右下角的 categoryName 保留：那是平台类目，与分组不是一回事。 */}
      </button>
    </article>
  );
}

export default ProductCard;
