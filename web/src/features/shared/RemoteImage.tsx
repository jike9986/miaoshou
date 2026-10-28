// 统一的商品图组件（design-system §5.7 图片区规格）
//
// 为什么需要它：商品图来自 alicdn / 妙手 CDN，这些 CDN 对 Referer 做 ACL，
// 从 127.0.0.1 或 file:// 打开时会被回 403（页面表现为「图片裂开」）。
// 加 referrerPolicy="no-referrer" 后不再发送 Referer，图片可正常加载；
// 同时统一 lazy/eager、object-cover、hover 缩放与失败占位，避免各页面各写一套。

import { useState } from "react";
import { ImageOff } from "lucide-react";
import { cn } from "@/lib/utils";

export interface RemoteImageProps {
  src: string | null | undefined;
  alt?: string;
  className?: string;
  /** 首屏前 N 张用 eager，其余 lazy（LCP 优化） */
  eager?: boolean;
  /** hover 时缩放 1.02（仅在 overflow-hidden 容器内使用） */
  zoomOnHover?: boolean;
  /** 容器额外类名（如 aspect-square） */
  wrapperClassName?: string;
  draggable?: boolean;
}

export function RemoteImage({
  src,
  alt = "",
  className,
  eager = false,
  zoomOnHover = false,
  wrapperClassName,
  draggable = false,
}: RemoteImageProps) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);

  if (!src) {
    return (
      <div
        className={cn(
          "grid size-full place-items-center bg-canvas-inset text-fg-disabled",
          wrapperClassName
        )}
      >
        <ImageOff className="size-6" strokeWidth={1.5} />
      </div>
    );
  }

  return (
    <div className={cn("relative size-full overflow-hidden bg-canvas-inset", wrapperClassName)}>
      {!loaded && !failed && <span aria-hidden className="absolute inset-0 skeleton-shimmer" />}
      {failed ? (
        <div className="grid size-full place-items-center text-fg-disabled">
          <ImageOff className="size-6" strokeWidth={1.5} />
        </div>
      ) : (
        <img
          src={src}
          alt={alt}
          loading={eager ? "eager" : "lazy"}
          decoding="async"
          draggable={draggable}
          referrerPolicy="no-referrer"
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
          className={cn(
            "size-full object-cover transition-transform duration-image ease-out",
            zoomOnHover && "group-hover:scale-[1.02]",
            loaded ? "opacity-100" : "opacity-0",
            className
          )}
        />
      )}
    </div>
  );
}

export default RemoteImage;
