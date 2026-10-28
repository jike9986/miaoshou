// 运行时引导：把本地 token 交给 `@/lib/api`（api.md §0.2 不落 localStorage）。
//
// 桌面端：Electron preload 注入 window.__DSH_BOOT__ = { apiBase, token, version }。
// 浏览器联调：没有 preload，dev server 通过 /__dev/token 把后端 secret.key 交给渲染层，
//            这里写入旧注入点别名 window.__AOHUANG_TOKEN__（api.ts 的兼容分支）。
// 生产构建：不做任何事（没有任何接口能读到 token）。

/** 引导是否成功（false 只影响错误提示文案，不阻塞渲染） */
export interface BootstrapResult {
  token: boolean;
  apiBase: string | null;
  /** 引导失败原因（UI 用它给出「请在桌面端打开」的提示） */
  reason: string | null;
}

let memo: Promise<BootstrapResult> | null = null;

function hasInjectedToken(): boolean {
  if (typeof window === "undefined") return false;
  if (window.__DSH_BOOT__?.token) return true;
  return Boolean(window.__AOHUANG_TOKEN__);
}

async function fetchDevToken(): Promise<string | null> {
  try {
    const response = await fetch("/__dev/token", {
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "omit",
    });
    if (!response.ok) return null;
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null && "token" in body) {
      const token = (body as { token?: unknown }).token;
      return typeof token === "string" && token ? token : null;
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * 在创建 React 树之前调用一次。
 * 幂等：重复调用返回同一个 Promise。
 */
export function bootstrapRuntime(): Promise<BootstrapResult> {
  if (memo) return memo;

  memo = (async (): Promise<BootstrapResult> => {
    if (typeof window === "undefined") {
      return { token: false, apiBase: null, reason: "非浏览器环境" };
    }
    const apiBase = window.__DSH_BOOT__?.apiBase ?? window.__AOHUANG_API_BASE__ ?? null;

    if (hasInjectedToken()) {
      return { token: true, apiBase, reason: null };
    }

    if (import.meta.env.DEV) {
      const token = await fetchDevToken();
      if (token) {
        window.__AOHUANG_TOKEN__ = token;
        return { token: true, apiBase, reason: null };
      }
      return {
        token: false,
        apiBase,
        reason: "浏览器预览模式未能读取本地令牌：请确认后端已启动（8812）后刷新页面",
      };
    }

    return {
      token: false,
      apiBase,
      reason: "缺少启动参数（window.__DSH_BOOT__.token），请在桌面端打开本应用",
    };
  })();

  return memo;
}

/** 同步读取当前是否已有可用 token（给错误态文案用） */
export function hasRuntimeToken(): boolean {
  return hasInjectedToken();
}
