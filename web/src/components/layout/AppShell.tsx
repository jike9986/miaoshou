// design-system §7.1 主框架（标题栏 40 + 侧边栏 232 + 内容区 24 内边距）
// 组合 TitleBar + Sidebar + 内容区（Outlet）；仅内容区滚动，标题栏与侧边栏固定。
// 路由切换动效（§6.2）：opacity 0→1 + y 8→0，220ms out，无退场动画。

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { motion, useReducedMotion } from "framer-motion";
import { Menu, Search, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { ease } from "@/lib/motion";
import { useIsNarrow } from "@/hooks/useMediaQuery";
import { isMac, useHotkey } from "@/hooks/useHotkey";
import type { VersionPayload } from "@/lib/queries";
import { Sidebar, type SidebarAccount, type SidebarNavGroup } from "@/components/layout/Sidebar";
import { TitleBar, TitleBarBreadcrumb, type TitleBarBrand } from "@/components/layout/TitleBar";

/* ------------------------------------------------------------------ *
 * 品牌标记（16px 图标位）：用老版 logo-mark.png（A 字标记），与 0.1.4 一致
 * ------------------------------------------------------------------ */

export function BrandMark({ className }: { className?: string }) {
  return (
    <img
      src="./logo-mark.png"
      alt=""
      aria-hidden
      draggable={false}
      className={cn("size-5 select-none object-contain", className)}
    />
  );
}

/* ------------------------------------------------------------------ *
 * 顶部全局搜索入口（§5.10 中部 h-7 w-[420px] rounded-sm；§5.4 Ctrl/Cmd+K）
 * ------------------------------------------------------------------ */

export function GlobalSearchTrigger({ onOpen }: { onOpen: () => void }) {
  const mac = isMac();
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        "flex h-7 w-[420px] items-center gap-2 rounded-sm border border-border bg-surface/70 px-2.5",
        "text-xs text-fg-muted transition-colors duration-fast",
        "hover:border-border-strong hover:bg-surface",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32"
      )}
    >
      <Search aria-hidden className="size-3.5 shrink-0" strokeWidth={2} />
      <span className="flex-1 truncate text-left">搜索商品、任务、店铺…</span>
      <kbd className="grid h-4 min-w-4 shrink-0 place-items-center rounded-xs border border-border bg-canvas-inset px-1 num text-[10px] leading-none text-fg-muted">
        {mac ? "⌘K" : "Ctrl K"}
      </kbd>
    </button>
  );
}

/* ------------------------------------------------------------------ *
 * AppShell
 * ------------------------------------------------------------------ */

export interface AppShellProps {
  /** 导航分组；默认使用 Sidebar 的 DEFAULT_NAV */
  nav?: SidebarNavGroup[];
  /** 标题栏左侧品牌 */
  brand?: TitleBarBrand;
  /** 底部账户区 */
  account?: SidebarAccount | null;
  onAccountClick?: () => void;
  /** 手动检查更新 */
  onCheckUpdate?: () => void;
  /** 是否正在检查更新 */
  checkingUpdate?: boolean;
  /** 版本信息（底部显示） */
  version?: VersionPayload | null;
  /** 标题栏中部（默认：全局搜索入口 > 面包屑） */
  titleBarCenter?: ReactNode;
  /** 标题栏右侧附加入口（最多 1 个） */
  titleBarExtra?: ReactNode;
  /** 全局搜索打开回调（Ctrl/Cmd+K） */
  onOpenSearch?: () => void;
  /** 路由 → 面包屑文案映射 */
  routeMeta?: Record<string, string[]>;
  className?: string;
}

export function AppShell({
  nav,
  brand,
  account,
  onAccountClick,
  onCheckUpdate,
  checkingUpdate,
  version,
  titleBarCenter,
  titleBarExtra,
  onOpenSearch,
  routeMeta,
  className,
}: AppShellProps) {
  const location = useLocation();
  const narrow = useIsNarrow();
  const reduced = useReducedMotion();
  const [collapsed, setCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  // 窄屏（< 1024）自动折叠侧边栏（§7.3 max-lg）
  useEffect(() => {
    setCollapsed(narrow);
  }, [narrow]);

  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  useHotkey("mod+k", () => onOpenSearch?.(), { enabled: Boolean(onOpenSearch) });

  const breadcrumb = useMemo(() => {
    if (!routeMeta) return null;
    const key = Object.keys(routeMeta).find(
      (path) => location.pathname === path || location.pathname.startsWith(`${path}/`)
    );
    if (!key) return null;
    const items = routeMeta[key];
    return items && items.length > 0 ? <TitleBarBreadcrumb items={items} /> : null;
  }, [location.pathname, routeMeta]);

  const center = titleBarCenter ?? (onOpenSearch ? <GlobalSearchTrigger onOpen={onOpenSearch} /> : breadcrumb);

  const sidebar = (
    <Sidebar
      nav={nav}
      collapsed={narrow ? false : collapsed}
      onCollapsedChange={setCollapsed}
      brand={{ icon: <BrandMark />, name: "奥皇上品" }}
      account={account}
      onAccountClick={onAccountClick}
      collapseTogglePosition="top"
      showCollapseToggle={!narrow}
      onNavigate={() => setDrawerOpen(false)}
      onCheckUpdate={onCheckUpdate}
      checking={checkingUpdate}
      version={version}
    />
  );

  return (
    <div
      className={cn("grid h-screen overflow-hidden bg-canvas", className)}
      style={{ gridTemplateRows: "var(--titlebar-h) 1fr" }}
    >
      <TitleBar
        brand={brand ?? { icon: <BrandMark className="size-4" />, name: "奥皇上品" }}
        center={center}
        extra={titleBarExtra}
      />

      <div className="flex min-h-0 overflow-hidden">
        {/* 桌面侧边栏（窄屏隐藏，改用抽屉） */}
        {!narrow && <div className="min-h-0 shrink-0 overflow-hidden">{sidebar}</div>}

        {/* 内容区：唯一滚动容器 */}
        <main id="app-content" className="flex min-w-0 flex-1 flex-col overflow-hidden">
          {narrow && (
            <div className="flex h-10 shrink-0 items-center gap-2 border-b border-border bg-canvas px-3">
              <button
                type="button"
                aria-label="打开导航"
                onClick={() => setDrawerOpen(true)}
                className="grid size-7 place-items-center rounded-sm text-fg-secondary transition-colors duration-fast hover:bg-surface-active"
              >
                <Menu className="size-4" strokeWidth={2} />
              </button>
              <span className="text-[13px] font-medium text-fg">奥皇上品</span>
            </div>
          )}
          <motion.div
            key={location.pathname}
            initial={reduced ? { opacity: 0 } : { opacity: 0, y: 8 }}
            animate={reduced ? { opacity: 1 } : { opacity: 1, y: 0 }}
            transition={{ duration: 0.22, ease: ease.out }}
            className="flex min-h-0 flex-1 flex-col overflow-hidden"
          >
            <Outlet />
          </motion.div>
        </main>
      </div>

      {/* 窄屏抽屉式侧边栏（§7.3 max-md：宽 280 + 遮罩） */}
      {narrow && drawerOpen && (
        <div className="fixed inset-0 z-[50] flex">
          <div
            className="absolute inset-0 bg-overlay/45 backdrop-blur-[2px]"
            onClick={() => setDrawerOpen(false)}
          />
          <motion.div
            initial={reduced ? { opacity: 0 } : { x: -24, opacity: 0 }}
            animate={reduced ? { opacity: 1 } : { x: 0, opacity: 1 }}
            transition={{ duration: 0.24, ease: ease.out }}
            className="relative h-full w-[280px] bg-sidebar shadow-3"
          >
            {sidebar}
            <button
              type="button"
              aria-label="关闭导航"
              onClick={() => setDrawerOpen(false)}
              className="absolute right-2 top-2 grid size-7 place-items-center rounded-sm text-fg-muted transition-colors duration-fast hover:bg-surface-active"
            >
              <X className="size-4" strokeWidth={2} />
            </button>
          </motion.div>
        </div>
      )}
    </div>
  );
}

export default AppShell;
