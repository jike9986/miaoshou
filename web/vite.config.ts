import fs from "node:fs";
import path from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

/**
 * 浏览器预览模式下的本地 token 交接。
 *
 * 桌面版由 Electron preload 注入 `window.__DSH_BOOT__.token`（api.md §0.2，不落 localStorage）。
 * 用浏览器跑前后端联调时没有 preload，这里在 dev server 上加一个只在本机回环可用的
 * `/__dev/token`，返回后端 `secret.key` 的内容，由 `src/lib/bootstrap.ts` 写到
 * `window.__AOHUANG_TOKEN__`（api.ts 里的旧注入点兼容别名）。
 *
 * 仅 dev 生效；生产构建没有任何 URL 能读到它。
 */
function readLocalToken(): string | null {
  const configured = process.env.AOHUANG_DATA_DIR;
  const localAppData = process.env.LOCALAPPDATA;
  const dataDir = configured ?? (localAppData ? path.join(localAppData, "aohuang-desktop") : null);
  if (!dataDir) return null;
  try {
    const token = fs.readFileSync(path.join(dataDir, "secret.key"), "utf8").trim();
    return token || null;
  } catch {
    return null;
  }
}

function devTokenPlugin(): Plugin {
  return {
    name: "aohuang-dev-token",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/__dev/token", (request, response) => {
        const host = request.headers.host ?? "";
        // 只允许本机回环访问，避免 dev server 绑到 0.0.0.0 时泄漏令牌
        if (!/^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/.test(host)) {
          response.statusCode = 403;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.end(JSON.stringify({ error: "forbidden-host" }));
          return;
        }
        const token = readLocalToken();
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        response.setHeader("Cache-Control", "no-store");
        response.statusCode = token ? 200 : 404;
        response.end(JSON.stringify(token ? { token } : { error: "token-not-found" }));
      });
    },
  };
}

export default defineConfig({
  // Electron 用 file:// 加载，绝对路径 /assets 会 404
  base: "./",
  plugins: [react(), devTokenPlugin()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    watch: {
      // 编辑器/工具用「写临时文件再原子替换」保存时会留下 *.<pid>.<uuid>.tmpdir 目录，
      // chokidar 去 watch 它们会抛 EBUSY 直接把 dev server 打挂（Windows 上可复现）。
      // 构建产物与冒烟截图目录也不需要监听（截图已统一到 docs/screenshots/，
      // 旧的 shots/ 名字一并留着，避免历史脚本再写出该目录时又把 dev server 打挂）。
      ignored: [
        "**/*.tmpdir/**",
        "**/shots/**",
        "**/screenshots/**",
        "**/dist/**",
        "**/.vite/**",
        "**/.smoke/**",
      ],
    },
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      // 后端会从 8812 起挑空闲端口（server/main.py 的 pick_port），dev 时如后端换到
      // 8813 用 AOHUANG_API_PORT=8813 pnpm dev 覆盖即可，不用改代码。
      "/api": {
        target: `http://127.0.0.1:${process.env.AOHUANG_API_PORT ?? "8812"}`,
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
