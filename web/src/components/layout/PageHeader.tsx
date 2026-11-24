// design-system §7.1 页面头（56px，px-6，border-b，bg-canvas/85 backdrop-blur-[2px]，sticky top-0 z-10）
// 标题 18/26 600；不再渲染面包屑（侧栏已承担导航定位，页头只留页名）；右侧操作区 gap-2，主按钮 md。

import { useLayoutEffect, useRef, type ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface PageHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  /** 右侧操作区（主按钮在右，同屏只允许一个 primary） */
  actions?: ReactNode;
  /** 标题下方的扩展区（筛选栏、Tab） */
  children?: ReactNode;
  className?: string;
  /** 底部是否显示 1px 分割线（默认显示） */
  bordered?: boolean;
}

export function PageHeader({
  title,
  description,
  actions,
  children,
  className,
  bordered = true,
}: PageHeaderProps) {
  return (
    <header
      className={cn(
        "sticky top-0 z-10 shrink-0 bg-canvas/85 backdrop-blur-[2px]",
        bordered && "border-b border-border",
        className
      )}
    >
      <div className="flex h-14 items-center gap-4 px-6">
        <div className="flex min-w-0 flex-1 items-baseline gap-3">
          <h1 className="truncate text-[18px] font-semibold leading-[26px] text-fg">{title}</h1>
          {description && (
            <p className="truncate text-[13px] leading-5 text-fg-muted">{description}</p>
          )}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      {children && <div className="px-6 pb-3">{children}</div>}
    </header>
  );
}

/** 页面内容容器：唯一滚动容器 + 24px 内边距（§7.1） */
export interface PageContentProps {
  children: ReactNode;
  className?: string;
  /** 内容最大宽度：表格页 1600 居中 / 表单页 880 左对齐 */
  width?: "table" | "form" | "full";
  /** 是否允许滚动（表格页自管理滚动时传 false） */
  scroll?: boolean;
  padding?: string;
  /**
   * 滚动位置记忆的 key。传了就自动「离开时存、回来时恢复」。
   *
   * 为什么需要：列表页点进详情是路由跳转，整个页面卸载，回来时滚动容器
   * 是全新的，位置自然归零。运营翻到第 80 件点进去看一眼，退回来又得从头
   * 往下滑 —— 用 sessionStorage 存偏移量，回来直接停在原处
   * （负责人 2026-11-23：「往下滑点进去一个产品的详情，再退出就会自动
   * 回到最上面」）。
   */
  scrollRestoreKey?: string;
}

/** 恢复滚动位置的全局前缀，避免和别的 key 撞车 */
const SCROLL_PREFIX = "dsh.scroll.";

export function PageContent({
  children,
  className,
  width = "table",
  scroll = true,
  padding = "p-6",
  scrollRestoreKey,
}: PageContentProps) {
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  // 挂载时恢复、卸载时保存。只存一次（rAF 里读），避免滚动事件写爆 storage。
  useLayoutEffect(() => {
    if (!scrollRestoreKey) return;
    const node = scrollerRef.current;
    if (!node) return;
    const storageKey = SCROLL_PREFIX + scrollRestoreKey;
    const saved = Number(window.sessionStorage.getItem(storageKey) || 0);
    if (saved > 0) {
      // 等布局稳定再设，否则内容还没撑开、scrollTop 会被夹到 0
      requestAnimationFrame(() => {
        if (scrollerRef.current) scrollerRef.current.scrollTop = saved;
      });
    }
    return () => {
      // 卸载时把当前偏移记下来，下次进来恢复
      window.sessionStorage.setItem(storageKey, String(node.scrollTop));
    };
  }, [scrollRestoreKey]);
  // 注意：Tailwind 对 w-[var(--x)] 会误判为颜色类型而不生成 CSS，
  // 这里直接写字面值（与 tokens.css 的 --form-max/--content-max 保持同值）。
  const widthClass =
    width === "form" ? "max-w-[880px]" : width === "table" ? "mx-auto max-w-[1760px]" : "";
  return (
    <div
      ref={scrollerRef}
      className={cn("min-h-0 flex-1", scroll && "overflow-y-auto", padding, className)}
    >
      <div className={cn("flex flex-col gap-4", widthClass)}>{children}</div>
    </div>
  );
}

export default PageHeader;
