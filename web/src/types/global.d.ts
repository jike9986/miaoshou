// design-system §0 落地基线 · §5.10 自定义标题栏
// api.md §0.2 认证（token 由 Electron preload 注入，不落 localStorage）
// architecture §1.3 运行时事实（apiBase 由 preload 提供，dev 走 Vite 代理）

export {};

declare global {
  /** preload.ts 暴露的窗口控制桥（app/src/preload.ts） */
  interface DesktopBridge {
    minimize: () => Promise<void>;
    toggleMaximize: () => Promise<void>;
    close: () => Promise<void>;
    isMaximized: () => Promise<boolean>;
    platform: string;
  }

  /**
   * Electron preload 注入的启动参数。
   * apiBase 形如 `http://127.0.0.1:8812`（不带 /api/v1，由 client 拼接）；
   * dev 模式下可能只注入 token，apiBase 为空，请求走 Vite 的 /api 代理。
   */
  interface DshBoot {
    apiBase?: string;
    token?: string;
    version?: string;
    platform?: string;
    dev?: boolean;
  }

  interface Window {
    desktop?: DesktopBridge;
    __DSH_BOOT__?: DshBoot;
    /** 旧注入点的兼容别名（api.md §0.2 兼容旧客户端） */
    __AOHUANG_TOKEN__?: string;
    __AOHUANG_API_BASE__?: string;
  }
}

export type { DesktopBridge, DshBoot };
