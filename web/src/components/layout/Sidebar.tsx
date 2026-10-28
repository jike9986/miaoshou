// design-system §5.9 侧边栏导航 · §7.1 布局（232px / 折叠 64px）· §6.2 折叠动画 240ms out
// 导航项高 36px，px-2.5 rounded-md gap-2.5 text-[13px] font-medium，图标 18px；
// 当前项 bg-white text-brand-700 shadow-1 + 左侧 2px 品牌色指示条；分组标题 11/16 500 letter-spacing .04em。

import { useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import {
  CalendarClock,
  Download,
  KeyRound,
  MoreHorizontal,
  RefreshCw,
  PackageSearch,
  PanelLeft,
  Play,
  ShieldBan,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useDownloadUpdate, useUpdateStatus, type VersionPayload } from "@/lib/queries";
import { apiErrorMessage, errorCode } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { toast } from "@/components/ui/toast";
import { Tooltip } from "@/components/ui/tooltip";

/* ------------------------------------------------------------------ *
 * 更新：字节 → MB（保留 1 位）
 * ------------------------------------------------------------------ */

/** 安装包大小给用户看只到 0.1 MB 就够；拿不到字节数时返回 null，不显示假的 0.0 MB */
function formatMB(bytes?: number | null): string | null {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes <= 0) return null;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 从 Windows / POSIX 路径里取文件名（pendingFile 后端给的是本地绝对路径） */
function baseName(path?: string | null): string | null {
  if (!path) return null;
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : null;
}

/* ------------------------------------------------------------------ *
 * 导航配置（对应 api.md 的资源分组：功能 / 系统）
 * ------------------------------------------------------------------ */

export interface SidebarNavItem {
  /** 路由路径（React Router） */
  to: string;
  label: string;
  icon: LucideIcon;
  /** 右侧计数徽标（>99 显示 99+） */
  count?: number | null;
  /** 禁用（如未连接账号时的入口） */
  disabled?: boolean;
  /** 二级导航缩进 */
  indent?: boolean;
}

export interface SidebarNavGroup {
  /** 分组标题（折叠时隐藏，改为 1px 分割线） */
  title: string;
  items: SidebarNavItem[];
}

/**
 * 默认导航。
 *
 * ⚠️ **改这里等于改所有没显式传 `nav` 的侧边栏。**
 *
 * 这个数组曾经是「图片翻译/采集箱又回来了」的真正源头：`App.tsx` 传了自己的
 * `NAV`（5 项，正确），把这个默认值覆盖掉了，所以界面上看不见；但只要有人
 * 新写一个 `<Sidebar />` 或 `<AppShell />` 而不传 `nav`，那四项立刻就会被
 * 渲染出来——其中 `/collect-box`、`/publish`、`/images` 三条点下去还会被
 * 路由重定向到别处。用户看到的是「明明删了怎么又出现了」，而代码里没有任何
 * 一处是「新加的」。
 *
 * 所以这里必须与 `App.tsx` 的 `NAV` 保持一致：**只留真实存在的页面**。
 * 采集箱是自动化页的数据源、图片翻译是处理设置里的开关，都不是独立页面。
 */
export const DEFAULT_NAV: SidebarNavGroup[] = [
  {
    title: "功能",
    items: [
      { to: "/automation", label: "自动化上品", icon: Zap },
      { to: "/warehouse", label: "成品仓库", icon: PackageSearch },
      { to: "/schedules", label: "定时发布", icon: CalendarClock },
      { to: "/words", label: "违规词库", icon: ShieldBan },
      { to: "/unlock", label: "连接妙手", icon: KeyRound },
    ],
  },
];

/* ------------------------------------------------------------------ *
 * 导航项
 * ------------------------------------------------------------------ */

export interface SidebarItemProps {
  item: SidebarNavItem;
  collapsed?: boolean;
  onNavigate?: () => void;
}

export function SidebarItem({ item, collapsed = false, onNavigate }: SidebarItemProps) {
  const Icon = item.icon;
  const count = item.count ?? null;
  const countText = count !== null && count > 99 ? "99+" : count;

  const link = (
    <NavLink
      to={item.to}
      onClick={onNavigate}
      aria-disabled={item.disabled || undefined}
      tabIndex={item.disabled ? -1 : undefined}
      className={({ isActive }) =>
        cn(
          "group relative flex h-9 items-center rounded-md text-[13px] font-medium leading-5",
          "transition-colors duration-fast ease-in-out",
          collapsed ? "justify-center px-0" : "gap-2.5 px-2.5",
          item.indent && !collapsed && "ml-4",
          item.disabled && "pointer-events-none opacity-50",
          isActive
            ? "bg-white text-brand-700 shadow-1"
            : "text-fg-secondary hover:bg-black/[0.04] hover:text-fg active:bg-black/[0.06]",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32"
        )
      }
    >
      {({ isActive }) => (
        <>
          {isActive && (
            <span aria-hidden className="absolute left-0 h-4 w-[2px] rounded-full bg-brand-600" />
          )}
          <Icon
            aria-hidden
            className={cn(
              "size-[18px] shrink-0",
              isActive ? "text-brand-600" : "text-fg-muted group-hover:text-fg-secondary"
            )}
            strokeWidth={1.75}
          />
          {!collapsed && (
            <>
              <span className="min-w-0 flex-1 truncate">{item.label}</span>
              {countText !== null && (
                <span
                  className={cn(
                    "ml-auto grid h-4 min-w-4 shrink-0 place-items-center rounded-xs px-1 num text-[11px] leading-4",
                    isActive ? "bg-brand-100 text-brand-800" : "bg-canvas-inset text-fg-muted"
                  )}
                >
                  {countText}
                </span>
              )}
            </>
          )}
        </>
      )}
    </NavLink>
  );

  if (collapsed) {
    return (
      <Tooltip content={item.label} side="right" delay={400} triggerClassName="w-full justify-center">
        <div className="w-full">{link}</div>
      </Tooltip>
    );
  }

  return link;
}

/* ------------------------------------------------------------------ *
 * 更新：下载 / 安装
 *
 * 拆成独立组件：下载结果（校验状态、字节数）是这份 UI 私有的状态，
 * 塞进 Sidebar 会让整个侧边栏跟着重渲染，也让 Sidebar 多背一层更新语义。
 * ------------------------------------------------------------------ */

interface UpdateActionsProps {
  version?: VersionPayload | null;
  /** 折叠态没有横向空间，只留一个图标按钮 */
  collapsed?: boolean;
}

function UpdateActions({ version, collapsed = false }: UpdateActionsProps) {
  const updateAvailable = Boolean(version?.updateAvailable);
  // 只在真有新版本时才去查 canInstall / readOnly：这个接口内部会拉一次清单，
  // 没更新的时候每次开页都打一遍纯属浪费。
  const status = useUpdateStatus({ enabled: updateAvailable });
  // 下载和安装各用一个实例，否则 isPending 会互相串：下载时「安装」按钮也会转圈。
  const download = useDownloadUpdate();
  const install = useDownloadUpdate();
  // 本次会话内下载的结果；pendingFile 只用来在下载前提示「上次那份还在」
  const [downloaded, setDownloaded] = useState<{ verified: boolean; size?: number } | null>(null);

  if (!updateAvailable) return null;

  const readOnly = status.data?.readOnly === true;
  const canInstall = status.data?.canInstall === true;
  const busy = download.isPending || install.isPending;
  // 只有本次会话真的下完才切到「安装并重启」：后端 POST 每次都重新下载校验，
  // 光凭 pendingFile 就写「已下载、可直接安装」会让用户以为不再走网络。
  const hasPackage = downloaded !== null;
  const pendingName = baseName(status.data?.pendingFile);

  // 点不动的原因要写在按钮上：等到点了才吃 403 / UNSUPPORTED_PLATFORM 是最差的体验
  const blockedReason = readOnly
    ? "只读模式下不能更新"
    : status.isPending
      ? "正在读取更新状态…"
      : status.isError
        ? apiErrorMessage(status.error)
        : !canInstall
          ? "当前平台不支持自动安装"
          : null;

  const handleDownload = (): void => {
    download.mutate(
      {},
      {
        onSuccess: (result) => {
          // 共享模式后端只回 accepted:false，没有 path / size，不能当成功提示
          if (result.downloaded !== true) {
            toast.info("没有下载安装包", {
              description: result.reason === "shared-mode" ? "共享模式下不下载更新包" : undefined,
            });
            return;
          }
          setDownloaded({ verified: result.sha256Verified === true, size: result.size });
          const size = formatMB(result.size);
          toast.success(
            result.sha256Verified === true ? "安装包已下载并校验通过" : "已下载（未提供校验值）",
            { description: size ? `安装包 ${size}` : undefined }
          );
        },
        onError: (error) => {
          // 校验失败的文件后端已经删了，必须让用户当场知道重下就能解决
          if (errorCode(error) === "DIGEST_MISMATCH") {
            toast.error(apiErrorMessage(error), { description: "安装包校验失败，已删除，请重试" });
            return;
          }
          toast.error(apiErrorMessage(error));
        },
      }
    );
  };

  const handleInstall = (): void => {
    // 不传 url / sha256：让后端按清单取，避免前端拼一个不在白名单里的地址被拒
    install.mutate(
      { install: true },
      {
        onSuccess: () => {
          // 安装器已经拉起，程序马上要退，这里不再调用任何会失效的界面逻辑
          toast.success("已启动安装程序", { description: "程序会退出并重新启动" });
        },
        onError: (error) => toast.error(apiErrorMessage(error)),
      }
    );
  };

  const size = formatMB(downloaded?.size);

  if (collapsed) {
    const label = hasPackage ? "安装并重启" : "下载并安装";
    const title = blockedReason ?? (hasPackage ? `${label}（当前窗口会关闭）` : label);
    return (
      <div className="mt-1 flex justify-center">
        <IconButton
          size="sm"
          label={title}
          title={title}
          disabled={busy || blockedReason !== null}
          icon={hasPackage ? <Play /> : <Download />}
          onClick={hasPackage ? handleInstall : handleDownload}
          className={cn(busy && "cursor-progress")}
        />
      </div>
    );
  }

  return (
    <div className="mt-1">
      {/* disabled 的 Button 带 pointer-events-none，鼠标事件落不到它身上，
          title 必须挂在父级 span 才能弹出「为什么点不了」 */}
      <span
        title={
          blockedReason ??
          (hasPackage ? "运行安装包并重启程序（当前窗口会关闭）" : undefined)
        }
        className="block"
      >
        {hasPackage ? (
          <Button
            size="sm"
            block
            loading={install.isPending}
            loadingText="正在安装…"
            disabled={busy || blockedReason !== null}
            onClick={handleInstall}
          >
            {install.isPending ? "正在安装…" : "安装并重启"}
          </Button>
        ) : (
          <Button
            size="sm"
            block
            variant="secondary"
            icon={<Download />}
            loading={download.isPending}
            loadingText="下载中…"
            disabled={busy || blockedReason !== null}
            onClick={handleDownload}
          >
            {download.isPending ? "下载中…" : "下载并安装"}
          </Button>
        )}
      </span>
      {hasPackage ? (
        <p className="mt-1 truncate text-[11px] leading-4 text-fg-muted">
          {downloaded?.verified ? "已校验" : "未校验"}
          {size ? ` · ${size}` : ""}
        </p>
      ) : (
        pendingName && (
          // 后端每次 POST 都会重新下载+校验，这里只提示「上次那份还在」，不承诺跳过网络
          <p className="mt-1 truncate text-[11px] leading-4 text-fg-muted">
            上次已下载：{pendingName}
          </p>
        )
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Sidebar
 * ------------------------------------------------------------------ */

export interface SidebarAccount {
  name: string;
  /** 店铺名 / 站点，11/16 muted */
  detail?: string;
  /** 当前操作员名（11px 更弱一行）：多人共用一台机器时，「现在是谁」必须随时可见 */
  operator?: string;
  initial?: string;
  avatarUrl?: string | null;
}

export interface SidebarProps {
  nav?: SidebarNavGroup[];
  /** 受控折叠 */
  collapsed?: boolean;
  defaultCollapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
  brand?: { icon?: ReactNode; name?: string };
  account?: SidebarAccount | null;
  onAccountClick?: () => void;
  collapseTogglePosition?: "top" | "bottom";
  /** 移动端抽屉内使用时关闭折叠按钮 */
  showCollapseToggle?: boolean;
  className?: string;
  onNavigate?: () => void;
  /** 手动检查更新（点了会强制去拉更新清单） */
  onCheckUpdate?: () => void;
  /** 是否正在检查 */
  checking?: boolean;
  /** 版本信息，用于底部那行提示 */
  version?: VersionPayload | null;
}

export function Sidebar({
  nav = DEFAULT_NAV,
  collapsed: collapsedProp,
  defaultCollapsed = false,
  onCollapsedChange,
  brand,
  account,
  onAccountClick,
  collapseTogglePosition = "bottom",
  showCollapseToggle = true,
  className,
  onNavigate,
  onCheckUpdate,
  checking = false,
  version,
}: SidebarProps) {
  const [internalCollapsed, setInternalCollapsed] = useState(defaultCollapsed);
  const collapsed = collapsedProp ?? internalCollapsed;

  // 底部那行提示：优先反映「有新版」，其次反映失败，最后显示当前版本
  const versionHint = version
    ? version.updateAvailable
      ? `发现新版本 ${version.latest}`
      : version.ok
        ? `已是最新 v${version.current}`
        : version.error
          ? "检查更新失败"
          : `v${version.current}`
    : "检查更新";

  const toggle = (): void => {
    const next = !collapsed;
    if (collapsedProp === undefined) setInternalCollapsed(next);
    onCollapsedChange?.(next);
  };

  return (
    <aside
      className={cn(
        "flex h-full shrink-0 flex-col border-r border-border bg-sidebar",
        "transition-[width] duration-medium ease-out",
        collapsed ? "w-16" : "w-[232px]",
        className
      )}
      data-collapsed={collapsed || undefined}
    >
      {/* 品牌区：logo 24px + 名称 15px（原 20/13 太弱，撑不起左上角第一视觉位）。
          收起按钮放这一行右侧 —— 桌面软件惯例（VSCode/Notion/飞书都在顶部），
          用户找「收起」时视线最先落在左上角。 */}
      <div className={cn("flex h-12 shrink-0 items-center gap-2 pt-2", collapsed ? "justify-center" : "px-3")}>
        <span className="grid size-6 shrink-0 place-items-center text-brand-600 [&_svg]:size-6 [&_img]:size-6">
          {brand?.icon}
        </span>
        {!collapsed && (
          <span className="truncate text-[15px] font-semibold leading-[22px] text-fg">
            {brand?.name ?? "奥皇上品"}
          </span>
        )}
        {collapseTogglePosition === "top" && showCollapseToggle && (
          <IconButton
            label={collapsed ? "展开侧边栏" : "收起侧边栏"}
            size="sm"
            icon={<PanelLeft className={cn(collapsed && "rotate-180")} />}
            onClick={toggle}
            className={cn(!collapsed && "ml-auto")}
          />
        )}
      </div>

      <nav aria-label="主导航" className="flex-1 overflow-y-auto px-2 py-2">
        {nav.map((group, groupIndex) => (
          <div key={group.title}>
            {collapsed ? (
              groupIndex > 0 && <div aria-hidden className="mx-1 my-2 h-px bg-border" />
            ) : (
              <p
                className={cn(
                  "px-2.5 text-2xs font-medium uppercase text-fg-muted",
                  groupIndex === 0 ? "mb-1.5" : "mb-1.5 mt-4"
                )}
                style={{ letterSpacing: "0.04em" }}
              >
                {group.title}
              </p>
            )}
            <div className="flex flex-col gap-0.5">
              {group.items.map((item) => (
                <SidebarItem key={item.to} item={item} collapsed={collapsed} onNavigate={onNavigate} />
              ))}
            </div>
          </div>
        ))}
      </nav>

      <div className="shrink-0">
        {showCollapseToggle && collapseTogglePosition === "bottom" && (
          <div className={cn("flex pb-1", collapsed ? "justify-center px-2" : "justify-end px-2")}>
            <IconButton
              label={collapsed ? "展开侧边栏" : "收起侧边栏"}
              size="sm"
              icon={<PanelLeft className={cn(collapsed && "rotate-180")} />}
              onClick={toggle}
            />
          </div>
        )}

        {/* 版本区：常态只显示「v0.1.5 · 已是最新」一行 11px 小字，
            有更新时整行变品牌色并浮出操作按钮 —— 平时不打扰，有事才醒目。 */}
        <div className={cn("border-t border-border", collapsed ? "py-2" : "px-2 py-1.5")}>
          <div className={cn("flex items-center", collapsed ? "justify-center px-0" : "gap-1")}>
            <button
              type="button"
              onClick={onCheckUpdate}
              disabled={checking}
              title={versionHint}
              className={cn(
                "flex min-w-0 items-center rounded-md text-left",
                "transition-colors duration-fast hover:bg-black/[0.04]",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32",
                "disabled:opacity-60",
                collapsed ? "size-6 justify-center" : "flex-1 gap-1.5 px-1.5 py-1"
              )}
            >
              <RefreshCw
                aria-hidden
                className={cn("size-3.5 shrink-0 text-fg-muted", checking && "animate-spin")}
                strokeWidth={1.75}
              />
              {!collapsed && (
                <span
                  className={cn(
                    "min-w-0 flex-1 truncate text-[11px] leading-4",
                    version?.updateAvailable ? "font-medium text-brand-700" : "text-fg-muted"
                  )}
                >
                  {versionHint}
                </span>
              )}
            </button>

            {/* 自动下载不可用时留一条手动通道：地址是清单给的，用户自己去浏览器下也能装 */}
            {!collapsed && version?.updateAvailable && version.url && (
              <button
                type="button"
                onClick={() => {
                  if (version.url) window.open(version.url, "_blank", "noopener,noreferrer");
                }}
                title="在浏览器里手动下载安装包"
                className="shrink-0 rounded-md bg-brand-600 px-1.5 py-0.5 text-[11px] font-medium text-white transition-colors duration-fast hover:bg-brand-700"
              >
                手动下载
              </button>
            )}
          </div>

          <UpdateActions version={version} collapsed={collapsed} />
        </div>

        {account && (
          <button
            type="button"
            onClick={onAccountClick}
            className={cn(
              "flex h-14 w-full items-center border-t border-border text-left",
              "transition-colors duration-fast hover:bg-black/[0.04]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-600/32",
              collapsed ? "justify-center px-0" : "gap-2 px-2"
            )}
          >
            <span className="grid size-6 shrink-0 place-items-center overflow-hidden rounded-full bg-brand-100 text-[11px] font-medium text-brand-800">
              {account.avatarUrl ? (
                <img src={account.avatarUrl} alt="" className="size-full object-cover" loading="lazy" />
              ) : (
                (account.initial ?? Array.from(account.name)[0] ?? "妙")
              )}
            </span>
            {!collapsed && (
              <>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-medium leading-5 text-fg">
                    {account.name}
                  </span>
                  {account.detail && (
                    <span className="block truncate text-[11px] leading-4 text-fg-muted">
                      {account.detail}
                    </span>
                  )}
                  {account.operator && (
                    <span className="block truncate text-[11px] leading-4 text-fg-muted">
                      操作员：{account.operator}
                    </span>
                  )}
                </span>
                <MoreHorizontal aria-hidden className="size-4 shrink-0 text-fg-muted" strokeWidth={1.75} />
              </>
            )}
          </button>
        )}
      </div>
    </aside>
  );
}

export default Sidebar;
