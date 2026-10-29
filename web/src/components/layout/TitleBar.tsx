// design-system §5.10 自定义标题栏（Electron 无边框窗口）· §7.1 布局（标题栏 40px）· §4.5 层级(30)
// 整条 drag，窗口按钮 no-drag；按钮 46×40，圆角 0，关闭 hover 为 danger-600（唯一允许的红色 hover 区）。
// 双击标题栏切换最大化；最大化图标跟随 isMaximized 变化。

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Copy, Minus, Square, X } from "lucide-react";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ *
 * window.desktop 桥（app/src/preload.ts）
 * ------------------------------------------------------------------ */

interface DesktopApi {
  minimize: () => Promise<void>;
  toggleMaximize: () => Promise<void>;
  close: () => Promise<void>;
  isMaximized: () => Promise<boolean>;
  platform: string;
}

function getDesktop(): DesktopApi | null {
  if (typeof window === "undefined") return null;
  const api = window.desktop;
  if (!api) return null;
  return api;
}

/** 是否运行在 Electron 壳内（浏览器里预览时窗口按钮降级为禁用） */
export function useDesktopApi(): { api: DesktopApi | null; available: boolean } {
  const [api, setApi] = useState<DesktopApi | null>(() => getDesktop());
  useEffect(() => {
    // preload 可能在首帧之后注入
    if (!api) setApi(getDesktop());
  }, [api]);
  return { api, available: api !== null };
}

/** 最大化状态订阅：打开时取一次 + 监听 resize（§5.10） */
export function useIsMaximized(): boolean {
  const [maximized, setMaximized] = useState(false);
  const { api } = useDesktopApi();

  useEffect(() => {
    if (!api) return;
    let alive = true;
    const sync = (): void => {
      api
        .isMaximized()
        .then((value) => {
          if (alive) setMaximized(value);
        })
        .catch(() => {
          /* 主进程未就绪时忽略 */
        });
    };
    sync();
    window.addEventListener("resize", sync);
    return () => {
      alive = false;
      window.removeEventListener("resize", sync);
    };
  }, [api]);

  return maximized;
}

/* ------------------------------------------------------------------ *
 * 窗口按钮（46×40，grid place-items-center，图标 pointer-events-none）
 * ------------------------------------------------------------------ */

type WindowButtonKind = "minimize" | "maximize" | "close";

const WINDOW_BUTTON_BASE =
  "no-drag grid h-10 w-[46px] place-items-center transition-colors duration-fast ease-in-out " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset " +
  "disabled:pointer-events-none disabled:opacity-40";

const WINDOW_BUTTON_STYLE: Record<WindowButtonKind, string> = {
  minimize:
    "text-fg-secondary hover:bg-surface-hover active:bg-surface-active focus-visible:ring-brand-600/40",
  maximize:
    "text-fg-secondary hover:bg-surface-hover active:bg-surface-active focus-visible:ring-brand-600/40",
  close:
    "text-fg-secondary hover:bg-danger-600 hover:text-white active:bg-danger-700 active:text-white focus-visible:ring-danger-600/40",
};

const WINDOW_BUTTON_LABEL: Record<WindowButtonKind, string> = {
  minimize: "最小化",
  maximize: "最大化",
  close: "关闭",
};

export interface WindowControlButtonProps {
  kind: WindowButtonKind;
  onClick: () => void;
  /** 最大化按钮在最大化时显示「还原」图标与标签 */
  maximized?: boolean;
  disabled?: boolean;
}

export function WindowControlButton({ kind, onClick, maximized = false, disabled }: WindowControlButtonProps) {
  // 图标尺寸按规格：最小化 10px、最大化/还原 9px、关闭 10px，stroke 1.5
  const icon =
    kind === "minimize" ? (
      <Minus size={10} strokeWidth={1.5} aria-hidden className="pointer-events-none" />
    ) : kind === "maximize" ? (
      maximized ? (
        <Copy size={9} strokeWidth={1.5} aria-hidden className="pointer-events-none" />
      ) : (
        <Square size={9} strokeWidth={1.5} aria-hidden className="pointer-events-none" />
      )
    ) : (
      <X size={10} strokeWidth={1.5} aria-hidden className="pointer-events-none" />
    );

  const label =
    kind === "maximize" ? (maximized ? "还原" : WINDOW_BUTTON_LABEL.maximize) : WINDOW_BUTTON_LABEL[kind];

  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className={cn(WINDOW_BUTTON_BASE, WINDOW_BUTTON_STYLE[kind])}
    >
      {icon}
    </button>
  );
}

/* ------------------------------------------------------------------ *
 * TitleBar
 * ------------------------------------------------------------------ */

export interface TitleBarBrand {
  /** 图标（16px） */
  icon?: ReactNode;
  /** 应用名，12.5px/500 text-fg-secondary */
  name?: ReactNode;
}

export interface TitleBarProps {
  brand?: TitleBarBrand;
  /** 中部内容：全局搜索入口或面包屑（12px muted，分隔符 /） */
  center?: ReactNode;
  /** 品牌与按钮之间、靠右的附加操作（最多 1 个全局入口，§5.10） */
  extra?: ReactNode;
  /** 是否显示右侧窗口按钮（macOS 或将来自定义时关闭） */
  showWindowControls?: boolean;
  className?: string;
}

export function TitleBar({
  center,
  extra,
  showWindowControls = true,
  className,
}: TitleBarProps) {
  const { api } = useDesktopApi();
  const maximized = useIsMaximized();

  const minimize = useCallback(() => {
    void api?.minimize();
  }, [api]);

  const toggleMaximize = useCallback(() => {
    void api?.toggleMaximize();
  }, [api]);

  const close = useCallback(() => {
    void api?.close();
  }, [api]);

  return (
    <header
      className={cn(
        "drag-region relative z-[30] flex h-[var(--titlebar-h)] w-full shrink-0 items-center",
        "border-b border-border bg-sidebar select-none",
        className
      )}
      onDoubleClick={(event) => {
        // 仅空白拖拽区响应，按钮区域是 no-drag，不会冒泡到这里
        if ((event.target as HTMLElement).closest(".no-drag")) return;
        toggleMaximize();
      }}
    >
      {/* 左侧不再显示品牌：左侧导航栏已经有 logo + 「奥皇上品」，
          标题栏再放一份就是上下重复。标题栏只保留窗口按钮。 */}

      {/* 中部：绝对居中，不参与 flex 挤压 */}
      {center && (
        <div className="pointer-events-none absolute left-1/2 top-1/2 hidden -translate-x-1/2 -translate-y-1/2 md:block">
          <div className="no-drag pointer-events-auto">{center}</div>
        </div>
      )}

      <div className="ml-auto flex items-center">
        {extra && <div className="no-drag flex items-center pr-2">{extra}</div>}
        {/* 没有 Electron 壳（浏览器预览）时不渲染窗口按钮。
            以前是 disabled 灰着摆在那里，看着像三个假按钮 —— 那是页面不是软件。 */}
        {showWindowControls && api && (
          <div className="flex items-stretch">
            <WindowControlButton kind="minimize" onClick={minimize} />
            <WindowControlButton
              kind="maximize"
              onClick={toggleMaximize}
              maximized={maximized}
            />
            <WindowControlButton kind="close" onClick={close} />
          </div>
        )}
      </div>
    </header>
  );
}

/** 标题栏面包屑（12px muted，分隔符 /，§5.10） */
export function TitleBarBreadcrumb({ items }: { items: string[] }) {
  return (
    <nav aria-label="面包屑" className="flex items-center gap-1.5 text-xs leading-[18px] text-fg-muted">
      {items.map((item, index) => (
        <span key={`${item}-${index}`} className="flex items-center gap-1.5">
          {index > 0 && <span aria-hidden className="text-border-strong">/</span>}
          <span className={cn(index === items.length - 1 && "text-fg-secondary")}>{item}</span>
        </span>
      ))}
    </nav>
  );
}

export default TitleBar;
