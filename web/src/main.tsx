// design-system §0 落地基线 · §8.1 文件落地顺序（tokens.css 在 main.tsx 引入）
// 挂载 React + QueryClientProvider + Sonner Toaster（App 内）+ 路由。
// 引导顺序：先 bootstrapRuntime() 拿到本地 token（桌面端 preload / 浏览器 dev 桥），
// 再渲染树 —— 否则首屏请求会因缺 token 报 BOOT_MISSING，白跑一次错误态。

import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, HashRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "@/App";
import { bootstrapRuntime } from "@/lib/bootstrap";
import "@/styles/tokens.css";
import "@/styles/tokens-additions.css";
import "@/styles/animations.css";

/** 查询客户端：§4.6 前端状态分层（服务端状态走 TanStack Query，写操作精确失效） */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      gcTime: 5 * 60_000,
      // 本地服务不重试 4xx（校验/只读/不存在需要立即反馈）
      retry: (failureCount, error) => {
        const code =
          typeof error === "object" && error !== null && "code" in error
            ? String((error as { code: unknown }).code)
            : "";
        if (
          code === "VALIDATION_ERROR" ||
          code === "AUTH_REQUIRED" ||
          code === "TOKEN_INVALID" ||
          code === "BOOT_MISSING" ||
          code === "FORBIDDEN_ORIGIN" ||
          code === "READ_ONLY_MODE" ||
          code === "NOT_FOUND" ||
          code === "CONFIRM_REQUIRED"
        ) {
          return false;
        }
        return failureCount < 2;
      },
      refetchOnWindowFocus: false,
    },
    /**
     * 写操作不重试：失败立即反馈，静默重试会造成「点一次提交两次」。
     *
     * 这里**刻意不设全局 onError**。曾经想加一个「兜底 toast」，
     * 但 TanStack Query 的语义是：`mutate(vars, { onError })` 的调用点回调
     * 与 hook 里的 `onError` **都会执行**，不是覆盖关系。而本项目 57 处调用点里
     * 有 54 处自己传了 onError —— 加全局兜底的结果是**每个失败都弹两个 toast**，
     * 比漏提示更烦人。
     *
     * 正确做法是在漏掉的那几个 hook 上单独补（已补 `useSelectTemplate` 的调用点）。
     * 剩余欠账见 docs/交付报告-0.1.5.md 的 P1 清单。
     */
    mutations: { retry: false },
  },
});

// Electron 用 file:// 加载页面，没有服务端路由：桌面端必须走 hash 路由，
// 浏览器开发/预览仍用 BrowserRouter，保留干净的 URL。
const isFileProtocol =
  typeof window !== "undefined" && window.location.protocol === "file:";
const Router = isFileProtocol ? HashRouter : BrowserRouter;

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("#root not found");

const root = ReactDOM.createRoot(rootElement);

void bootstrapRuntime().finally(() => {
  root.render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        {/* Electron 用 file:// 加载，没有服务端路由；桌面端必须走 hash 路由，
            浏览器开发/预览仍用 BrowserRouter 以保留干净的 URL。 */}
        <Router>
          <App />
        </Router>
      </QueryClientProvider>
    </React.StrictMode>
  );
});
