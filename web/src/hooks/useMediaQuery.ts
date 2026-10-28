// design-system §7.3 响应式断点（sm 640 / md 768 / lg 1024 / xl 1280 / 2xl 1536 / 3xl 1680）
// 用于需要 JS 判断布局的场合（侧边栏折叠、商品卡列数、表格列显隐）。

import { useEffect, useState } from "react";

/** 与 tailwind.config.js 的 screens 保持一致 */
export const BREAKPOINTS = {
  sm: 640,
  md: 768,
  lg: 1024,
  xl: 1280,
  "2xl": 1536,
  "3xl": 1680,
} as const;

export type Breakpoint = keyof typeof BREAKPOINTS;

/** 通用媒体查询（SSR 安全，首帧返回 false） */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(query);
    const handler = (event: MediaQueryListEvent): void => setMatches(event.matches);
    setMatches(media.matches);
    // 兼容旧 Chromium（Electron 31 已支持 addEventListener，这里仍走 addListener 兜底）
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", handler);
      return () => media.removeEventListener("change", handler);
    }
    media.addListener(handler);
    return () => media.removeListener(handler);
  }, [query]);

  return matches;
}

/** 视口是否 ≥ 某断点 */
export function useBreakpoint(breakpoint: Breakpoint): boolean {
  return useMediaQuery(`(min-width: ${BREAKPOINTS[breakpoint]}px)`);
}

/** 当前宽度所属断点（取满足的最大断点） */
export function useCurrentBreakpoint(): Breakpoint | "xs" {
  const isSm = useBreakpoint("sm");
  const isMd = useBreakpoint("md");
  const isLg = useBreakpoint("lg");
  const isXl = useBreakpoint("xl");
  const is2xl = useBreakpoint("2xl");
  const is3xl = useBreakpoint("3xl");

  if (is3xl) return "3xl";
  if (is2xl) return "2xl";
  if (isXl) return "xl";
  if (isLg) return "lg";
  if (isMd) return "md";
  if (isSm) return "sm";
  return "xs";
}

/** 是否处于窄屏（< 1024，侧边栏自动折叠，§7.3 max-lg） */
export function useIsNarrow(): boolean {
  return useMediaQuery(`(max-width: ${BREAKPOINTS.lg - 1}px)`);
}

/** 用户是否要求减少动效（§6.4） */
export function usePrefersReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

/**
 * 商品卡列数（§7.2 商品卡列数表）
 * < 640 → 2 / 640–1023 → 2 / 1024–1279 → 3 / 1280–1599 → 4 / 1600–1919 → 5 / ≥1920 → 6
 */
export function useProductGridColumns(): number {
  const is1920 = useMediaQuery("(min-width: 1920px)");
  const is1600 = useMediaQuery("(min-width: 1600px)");
  const is1280 = useMediaQuery("(min-width: 1280px)");
  const is1024 = useMediaQuery("(min-width: 1024px)");
  if (is1920) return 6;
  if (is1600) return 5;
  if (is1280) return 4;
  if (is1024) return 3;
  return 2;
}
