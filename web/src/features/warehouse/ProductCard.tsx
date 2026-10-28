// features/warehouse —— 商品卡（design-system §5.7 商品卡规格）
// 图片 1:1 懒加载、标题恒 2 行、价格等宽数字、类目/分组标签、右上勾选框。

import { Images, Package, PencilLine, ShoppingBag } from "lucide-react";
import { cn, formatCount, formatPrice, formatTime } from "@/lib/utils";
import type { Product } from "@/lib/types";
import { productStatus } from "@/features/shared/status";
import { RemoteImage } from "@/features/shared/RemoteImage";
import { Badge, Checkbox, StatusBadge, StatusDot } from "@/components/ui";

export interface ProductCardProps {
  product: Product;
  selected: boolean;
  onToggle: (id: string) => void;
  onOpen?: (product: Product) => void;
  index: number;
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

export function ProductCard({ product, selected, onToggle, onOpen, index }: ProductCardProps) {
  const price = priceText(product);
  const statusMeta = productStatus(product.status);
  const groupName = product.group?.trim();

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
      <div className="relative aspect-square w-full overflow-hidden bg-canvas-inset">
        <RemoteImage
          src={product.image}
          eager={index < 8}
          zoomOnHover
          className="[&_svg]:size-8"
        />

        <StatusBadge
          meta={statusMeta}
          className="absolute left-2 top-2 bg-white/92 backdrop-blur-[2px]"
        />

        <div
          className={cn(
            "absolute right-2 top-2 rounded-xs bg-white/92 p-1 backdrop-blur-[2px]",
            "transition-opacity duration-fast ease-in-out",
            selected
              ? "opacity-100"
              : "opacity-0 group-hover:opacity-100 group-focus-within:opacity-100"
          )}
        >
          <Checkbox
            checked={selected}
            onChange={() => onToggle(product.id)}
            aria-label={`选择 ${product.title || product.detailId}`}
          />
        </div>

        {product.imageCount > 1 && (
          <span className="absolute bottom-2 right-2 inline-flex items-center gap-1 rounded-xs bg-overlay/55 px-1.5 py-0.5 text-2xs text-white">
            <Images className="size-3" strokeWidth={2} />
            <span className="num">{product.imageCount}</span>
          </span>
        )}
      </div>

      <button
        type="button"
        onClick={() => onOpen?.(product)}
        className="flex flex-1 flex-col gap-2 p-3 text-left focus-visible:outline-none"
      >
        <h3 className="line-clamp-2 min-h-[40px] text-sm font-medium leading-5 text-fg">
          {product.title || "（无标题）"}
        </h3>

        <p className="truncate font-mono text-2xs text-fg-muted">
          {product.detailId}
          {product.cid ? ` · cid ${product.cid}` : ""}
        </p>

        <div className="flex min-h-[24px] items-baseline gap-1.5">
          {price ? (
            <span className="num text-h3 font-semibold text-fg">
              <span className="mr-0.5 text-xs font-medium text-fg-secondary">{price.symbol}</span>
              {price.amount}
            </span>
          ) : (
            <span className="text-sm text-fg-muted">价格未录入</span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {product.categoryName && (
            <Badge variant="neutral" title={`类目 ${product.categoryName}`}>
              {product.categoryName}
            </Badge>
          )}
          {groupName && groupName !== "" && (
            <Badge variant="brand" title={`分组 ${groupName}`}>
              {groupName}
            </Badge>
          )}
          {!product.categoryName && !groupName && (
            <span className="text-2xs text-fg-muted">未分类</span>
          )}
        </div>

        <div className="flex items-center gap-3 text-xs text-fg-muted">
          <span className="flex items-center gap-1" title="SKU 数">
            <Package className="size-3" strokeWidth={1.75} />
            <span className="num">{formatCount(product.skuCount)}</span>
          </span>
          <span className="flex items-center gap-1" title="近 30 天销量">
            <ShoppingBag className="size-3" strokeWidth={1.75} />
            <span className="num">{formatCount(product.salesLast30)}</span>
          </span>
          {product.claimed && (
            <span className="ml-auto flex items-center gap-1">
              <StatusDot tone="warning" />
              <span>已认领</span>
            </span>
          )}
        </div>

        {/* 谁改的：只有后端真的给了 updatedBy 才显示。
            没有就不占位（老后端 / 本机未记名时这里不该冒出「未知」）。 */}
        {product.updatedBy ? (
          <p
            className="flex items-center gap-1 truncate text-2xs leading-4 text-fg-muted"
            title={`最后改动：${product.updatedBy}${
              product.updatedAt ? ` · ${formatTime(product.updatedAt)}` : ""
            }`}
          >
            <PencilLine className="size-3 shrink-0" strokeWidth={1.75} />
            <span className="truncate">
              {product.updatedBy}
              {product.updatedAt ? ` 编辑于 ${formatTime(product.updatedAt)}` : " 编辑过"}
            </span>
          </p>
        ) : null}
      </button>
    </article>
  );
}

export default ProductCard;
