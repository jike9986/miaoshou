import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { setTimeout } from "node:timers/promises";
import { URL } from "node:url";
import { runInNewContext } from "node:vm";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import ts from "typescript";
import * as apiTarget from "./apiTarget.ts";
import { queryKeys } from "./queryKeys.ts";

function compile(relativePath, appended = "") {
  return ts.transpileModule(readFileSync(new URL(relativePath, import.meta.url), "utf8"), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS },
  }).outputText + appended;
}

const appSource = compile("../App.tsx", "\nexports.Shell = Shell; exports.TokenNotice = TokenNotice;");
const queriesSource = compile("./queries.ts");

function appState({ runtimeToken, identity, apiBase }) {
  const calls = [];
  const imports = {
    react: { useEffect: () => {}, useRef: () => ({ current: null }) },
    "react/jsx-runtime": { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
    "react-router-dom": { useNavigate: () => () => {}, useLocation: () => ({ pathname: "/automation" }) },
    "@/lib/queries": {
      useAuthStatus: () => ({ data: {} }),
      useVersion: (...args) => { calls.push(args); return {}; },
    },
    "@/lib/bootstrap": { hasRuntimeToken: () => runtimeToken },
    "@/lib/api": { api: { get: () => new Promise(() => {}) }, resolveApiBase: () => apiBase },
    "@/lib/apiTarget": apiTarget,
    "@/lib/operator": { useOperatorIdentity: () => identity, operatorDisplayName: () => "Test" },
    "@/lib/featureFlags": { SHOW_ACCOUNT_UI: true },
  };
  const exported = {};
  runInNewContext(appSource, {
    exports: exported,
    require: (name) => imports[name] ?? {},
  });
  exported.Shell();
  return { calls, notice: exported.TokenNotice() };
}

test("HTTPS software sessions enable one existing automatic update query without a machine token", () => {
  const state = appState({
    runtimeToken: false, identity: { token: "software-session", role: "member" },
    apiBase: "https://aohuang888.com/miaoshou-platform",
  });
  assert.equal(JSON.stringify(state.calls), JSON.stringify([[true, true]]));
  assert.equal(state.notice, null);
});

test("login-before-auth stays quiet while a genuine local bootstrap failure retains its notice", () => {
  const remote = appState({
    runtimeToken: false, identity: null, apiBase: "https://aohuang888.com/miaoshou-platform",
  });
  assert.equal(JSON.stringify(remote.calls), JSON.stringify([[true, false]]));
  assert.equal(remote.notice, null);
  const local = appState({ runtimeToken: false, identity: null, apiBase: "http://127.0.0.1:8812" });
  assert.equal(JSON.stringify(local.calls), JSON.stringify([[true, false]]));
  assert.ok(local.notice);
});

test("local bootstrap credentials keep the existing automatic update query enabled", () => {
  const state = appState({ runtimeToken: true, identity: null, apiBase: "http://127.0.0.1:8812" });
  assert.equal(JSON.stringify(state.calls), JSON.stringify([[true, true]]));
  assert.equal(state.notice, null);
});

test("manual update checks reuse their response across active query variants without a second request", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const previous = { ok: true, current: "0.1.5", updateAvailable: false };
  const updated = { ...previous, latest: "0.1.6", updateAvailable: true };
  let reads = 0;
  let checks = 0;
  const stop = [];
  for (const refresh of [false, true]) {
    const key = queryKeys.system.version(refresh);
    client.setQueryData(key, previous);
    const observer = new QueryObserver(client, {
      queryKey: key, staleTime: Infinity,
      queryFn: async () => { reads += 1; return previous; },
    });
    stop.push(observer.subscribe(() => {}));
  }
  const exported = {};
  runInNewContext(queriesSource, {
    exports: exported,
    require: (name) => ({
      "@tanstack/react-query": { useQueryClient: () => client, useMutation: (options) => options },
      "@/lib/queryKeys": { queryKeys },
      "@/lib/api": { api: { post: async (path) => {
        assert.equal(path, "/system/update-check");
        checks += 1;
        return updated;
      } } },
    })[name] ?? {},
  });
  try {
    const mutation = exported.useCheckUpdate();
    await mutation.onSuccess(await mutation.mutationFn());
    await setTimeout(0);
    assert.equal(checks, 1);
    assert.equal(reads, 0);
    for (const refresh of [false, true]) {
      assert.deepEqual(client.getQueryData(queryKeys.system.version(refresh)), updated);
    }
  } finally {
    for (const unsubscribe of stop) unsubscribe();
    client.clear();
  }
});

test("remote updates download inside the app instead of opening a browser", () => {
  // 正式客户端连中心服务（remote=true）。后端在 shared_mode 下拒绝下载，
  // 所以远端模式改走 Electron 主进程的本地下载：https 白名单 + sha256 + 原子落盘。
  // 这里验证的是「有 desktop.update 通道时不再打开浏览器」。
  const calls = [];
  const downloads = [];
  const exported = {};
  runInNewContext(compile("../components/layout/Sidebar.tsx"), {
    exports: exported, URL,
    window: {
      open: (...args) => calls.push(args),
      desktop: {
        update: {
          pending: () => Promise.resolve(null),
          download: (manifest) => {
            downloads.push(manifest);
            return Promise.resolve({ ok: true, size: 81261956 });
          },
          install: () => Promise.resolve({ ok: true }),
          discard: () => Promise.resolve({ ok: true }),
          runInstallerNow: () => Promise.resolve({ ok: true }),
          onProgress: () => () => {},
        },
      },
    },
    require: (name) => ({
      react: {
        useState: () => [null, () => {}],
        useEffect: () => {},
      },
      "react/jsx-runtime": {
        jsx: (type, props) => ({ type, props }),
        jsxs: (type, props) => ({ type, props }),
      },
      "@/lib/api": { api: { get: () => new Promise(() => {}) }, resolveApiBase: () => "https://aohuang888.com/miaoshou-platform" },
      "@/lib/apiTarget": apiTarget,
      // 下载是异步的，回调里会弹 toast；测试环境没有 Toaster，
      // 不 mock 会在测试结束后抛 unhandledRejection。
      "@/components/ui/toast": {
        toast: { success() {}, error() {}, info() {}, warning() {} },
      },
      "@/lib/queries": {
        useUpdateStatus: (options) => { assert.equal(options.enabled, false); return {}; },
        useDownloadUpdate: () => ({ mutate: () => assert.fail("remote backend must not install on Linux") }),
      },
    })[name] ?? {},
  });
  const url = "https://aohuang888.com/miaoshou-downloads/setup.exe";
  const sha256 = "a".repeat(64);
  const button = exported.UpdateActions({
    version: { updateAvailable: true, url, sha256, latest: "0.1.10", sizeBytes: 81261956 },
  });
  // 返回的是外层 div（含按钮 + 进度条），按钮在 children 里
  const inner = button.props.children ?? button;
  const downloadButton = Array.isArray(inner) ? inner[0] : inner;
  assert.equal(downloadButton.props.disabled, false);
  downloadButton.props.onClick();
  // 关键断言：没有打开浏览器，而是交给了客户端下载通道
  assert.equal(calls.length, 0, "软件内更新不应该再打开浏览器");
  assert.equal(downloads.length, 1);
  assert.equal(downloads[0].url, url);
  assert.equal(downloads[0].sha256, sha256);

  for (const unsafe of ["http://aohuang888.com/a.exe", "https://evil.test/a.exe",
    "https://aohuang888.com:8443/a.exe", "https://user:pass@aohuang888.com/a.exe",
    "javascript:alert(1)", "https://aohuang888.com/page"]) {
    assert.equal(apiTarget.officialUpdateUrl(unsafe), null);
  }
});

test("remote updates fall back to the browser when no desktop bridge exists", () => {
  // 浏览器里打开（没有 preload 注入的 desktop.update）时，退回打开官方下载页。
  const calls = [];
  const exported = {};
  runInNewContext(compile("../components/layout/Sidebar.tsx"), {
    exports: exported, URL,
    window: { open: (...args) => calls.push(args) },
    require: (name) => ({
      react: { useState: () => [null, () => {}], useEffect: () => {} },
      "react/jsx-runtime": { jsx: (type, props) => ({ type, props }) },
      "@/lib/api": { api: { get: () => new Promise(() => {}) }, resolveApiBase: () => "https://aohuang888.com/miaoshou-platform" },
      "@/lib/apiTarget": apiTarget,
      "@/lib/queries": {
        useUpdateStatus: (options) => { assert.equal(options.enabled, false); return {}; },
        useDownloadUpdate: () => ({ mutate: () => assert.fail("remote backend must not install on Linux") }),
      },
    })[name] ?? {},
  });
  const url = "https://aohuang888.com/miaoshou-downloads/setup.exe";
  const button = exported.UpdateActions({ version: { updateAvailable: true, url } });
  button.props.onClick();
  assert.deepEqual(calls[0], [url, "_blank", "noopener,noreferrer"]);
  for (const unsafe of ["http://aohuang888.com/a.exe", "https://evil.test/a.exe",
    "https://aohuang888.com:8443/a.exe", "https://user:pass@aohuang888.com/a.exe",
    "javascript:alert(1)", "https://aohuang888.com/page"]) {
    assert.equal(exported.UpdateActions({ version: { updateAvailable: true, url: unsafe } }).props.disabled, true);
  }
});
