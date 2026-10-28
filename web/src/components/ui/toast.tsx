// design-system §5.11 Toast（sonner）· §6.2（进入 y 8→0 + scale .98→1 220ms out；退出 140ms in）
// 宽度 360、内边距 12/14、圆角 12；标题 13/20 500；描述 12/18 muted；图标色随类型。
// 规则：同一次用户动作只出一条 Toast；批量操作汇总为一条（带「查看详情」）。

import type { ReactElement, ReactNode } from "react";
import { Toaster as SonnerToaster, toast as sonnerToast, type ExternalToast } from "sonner";
import { AlertTriangle, CheckCircle2, Info, Loader2, XCircle } from "lucide-react";

/** 停留时长（§5.11：成功 3000 / 信息 4000 / 错误 6000；hover 暂停由 sonner 负责） */
export const TOAST_DURATION = {
  success: 3000,
  info: 4000,
  warning: 4000,
  error: 6000,
  loading: Number.POSITIVE_INFINITY,
} as const;

/** Toast 容器类（与 §5.11 规格逐条对应） */
const TOAST_CLASS =
  "w-[360px] max-w-[calc(100vw-32px)] rounded-lg border border-border bg-elevated p-3 pl-3.5 " +
  "shadow-3 text-sm text-fg gap-2.5 items-start";

const TITLE_CLASS = "text-[13px] font-medium leading-5 text-fg";
const DESC_CLASS = "mt-0.5 text-xs leading-[18px] text-fg-muted line-clamp-2";

export interface ToastOptions {
  /** 最多 2 行描述 */
  description?: ReactNode;
  /** 唯一动作（文字按钮，12px，brand-700） */
  action?: { label: string; onClick: () => void };
  /** 自定义停留时长 */
  duration?: number;
  /** 覆盖 id：同一 id 会替换上一条（批量操作汇总用） */
  id?: string;
  /** 关闭后的回调（收到 toast id） */
  onDismiss?: (id: number | string) => void;
}

function buildOptions(options: ToastOptions): ExternalToast {
  return {
    description: options.description,
    id: options.id,
    duration: options.duration,
    onDismiss: options.onDismiss ? (instance) => options.onDismiss?.(instance.id) : undefined,
    action: options.action
      ? {
          label: options.action.label,
          onClick: options.action.onClick,
        }
      : undefined,
  };
}

/** 统一 Toast 入口。所有提示都走这里，保证样式与时长一致。
 *  classNames 只在 <AppToaster> 的 toastOptions 里声明一次（sonner 会与单条配置合并，
 *  两处都写会出现重复类名）。
 *
 * toast.success("已上架 12 个商品", { description: "2 个失败", action: { label: "查看详情", onClick } })
 * const id = toast.loading("正在发布…"); toast.success("发布完成", { id })
 */
export const toast = {
  success(message: ReactNode, options: ToastOptions = {}) {
    return sonnerToast.success(message, {
      ...buildOptions({ duration: TOAST_DURATION.success, ...options }),
      duration: options.duration ?? TOAST_DURATION.success,
    });
  },

  error(message: ReactNode, options: ToastOptions = {}) {
    return sonnerToast.error(message, {
      ...buildOptions({ duration: TOAST_DURATION.error, ...options }),
      duration: options.duration ?? TOAST_DURATION.error,
    });
  },

  info(message: ReactNode, options: ToastOptions = {}) {
    return sonnerToast.info(message, {
      ...buildOptions({ duration: TOAST_DURATION.info, ...options }),
      duration: options.duration ?? TOAST_DURATION.info,
    });
  },

  warning(message: ReactNode, options: ToastOptions = {}) {
    return sonnerToast.warning(message, {
      ...buildOptions({ duration: TOAST_DURATION.warning, ...options }),
      duration: options.duration ?? TOAST_DURATION.warning,
    });
  },

  /** 不自动消失；用同一个 id 调 success/error 结束 */
  loading(message: ReactNode, options: ToastOptions = {}) {
    return sonnerToast.loading(message, {
      ...buildOptions(options),
      duration: options.duration ?? TOAST_DURATION.loading,
    });
  },

  /** 自定义内容（进度条、多行结果）；sonner 要求返回 ReactElement */
  custom(content: ReactElement, options: ToastOptions = {}) {
    return sonnerToast.custom(() => content, buildOptions(options));
  },

  /** 关闭指定 / 全部 */
  dismiss: sonnerToast.dismiss,
  promise: sonnerToast.promise,
};

/* ------------------------------------------------------------------ *
 * Toaster：挂在 App 根部（App.tsx）
 * ------------------------------------------------------------------ */

const ICON_SIZE = 16;

/** 图标色：成功 #0B7F57 / 警告 #B45309 / 错误 #C0364B / 信息 #1F6FEB（走 tokens） */
const ICONS = {
  success: <CheckCircle2 size={ICON_SIZE} className="text-success-600" strokeWidth={2} />,
  error: <XCircle size={ICON_SIZE} className="text-danger-600" strokeWidth={2} />,
  warning: <AlertTriangle size={ICON_SIZE} className="text-warning-600" strokeWidth={2} />,
  info: <Info size={ICON_SIZE} className="text-info-600" strokeWidth={2} />,
  loading: <Loader2 size={ICON_SIZE} className="animate-spin text-fg-muted" strokeWidth={2} />,
};

export interface AppToasterProps {
  position?: "top-left" | "top-right" | "bottom-left" | "bottom-right" | "top-center" | "bottom-center";
}

export function AppToaster({ position = "bottom-right" }: AppToasterProps) {
  return (
    <SonnerToaster
      position={position}
      offset={16}
      gap={8}
      visibleToasts={3}
      duration={TOAST_DURATION.info}
      closeButton
      expand={false}
      toastOptions={{
        className: TOAST_CLASS,
        classNames: {
          title: TITLE_CLASS,
          description: DESC_CLASS,
          actionButton: "text-xs font-medium text-brand-700 hover:text-brand-800 bg-transparent",
          closeButton:
            "size-6 rounded-sm bg-elevated text-fg-muted opacity-0 group-hover:opacity-100 transition-opacity duration-fast",
        },
      }}
      icons={ICONS}
    />
  );
}

export { sonnerToast };
