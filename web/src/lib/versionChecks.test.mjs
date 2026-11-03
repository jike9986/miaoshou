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
    "@/lib/api": { resolveApiBase: () => apiBase },
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
