// design-system §5.11 Toast —— hook 版封装，便于业务组件按需取
// 与 "@/components/ui/toast" 的 toast 对象是同一实现，二选一即可。

import { useMemo, type ReactNode } from "react";
import { toast as baseToast, type ToastOptions } from "@/components/ui/toast";

export interface UseToastResult {
  success: (message: ReactNode, options?: ToastOptions) => string | number;
  error: (message: ReactNode, options?: ToastOptions) => string | number;
  info: (message: ReactNode, options?: ToastOptions) => string | number;
  warning: (message: ReactNode, options?: ToastOptions) => string | number;
  loading: (message: ReactNode, options?: ToastOptions) => string | number;
  dismiss: (id?: string | number) => void;
  /** 批量操作汇总：一条 Toast 报告成功/失败数（§5.11 规则） */
  batch: (params: { verb: string; success: number; failed?: number; onDetail?: () => void }) => void;
}

export function useToast(): UseToastResult {
  return useMemo<UseToastResult>(() => {
    return {
      success: (message, options) => baseToast.success(message, options),
      error: (message, options) => baseToast.error(message, options),
      info: (message, options) => baseToast.info(message, options),
      warning: (message, options) => baseToast.warning(message, options),
      loading: (message, options) => baseToast.loading(message, options),
      dismiss: (id) => baseToast.dismiss(id),
      batch: ({ verb, success, failed = 0, onDetail }) => {
        const message = failed > 0 ? `${verb} ${success} 个，${failed} 个失败` : `${verb} ${success} 个`;
        const options: ToastOptions = {
          description: failed > 0 ? "失败项已保留原始数据，可重试。" : undefined,
          action: failed > 0 && onDetail ? { label: "查看详情", onClick: onDetail } : undefined,
        };
        if (failed > 0) baseToast.warning(message, options);
        else baseToast.success(message, options);
      },
    };
  }, []);
}

export default useToast;
