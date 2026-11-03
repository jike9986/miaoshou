import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { URL } from "node:url";
import { runInNewContext } from "node:vm";
import ts from "typescript";

function renderModule(relativePath, identity, from = "/automation") {
  const navigations = [];
  const jsx = (type, props) => ({ type, props });
  const exported = {};
  const source = ts.transpileModule(readFileSync(new URL(relativePath, import.meta.url), "utf8"), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const imports = {
    react: {
      useEffect: (effect) => effect(), useRef: () => ({ current: null }),
      useMemo: (factory) => factory(), useCallback: (callback) => callback,
    },
    "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: "Fragment" },
    "react-router-dom": {
      Navigate: "Navigate", Outlet: "Outlet", Route: "Route", Routes: "Routes",
      useNavigate: () => (to) => navigations.push(to),
      useLocation: () => ({ pathname: "/login", state: { from } }),
    },
    "@/lib/operator": { useOperatorIdentity: () => identity, operatorDisplayName: () => "Test" },
    "@/lib/queries": {
      useAuthStatus: () => ({ data: { connected: true } }),
      useVersion: () => ({}), useSoftwareCredentialsStatus: () => ({}),
    },
    "@/lib/bootstrap": { hasRuntimeToken: () => false },
    "@/lib/api": { resolveApiBase: () => "https://aohuang888.com/miaoshou-platform" },
    "@/lib/apiTarget": { shouldShowLocalTokenNotice: () => false },
    "@/lib/featureFlags": { SHOW_ACCOUNT_UI: true },
  };
  runInNewContext(source + "\nexports.Shell = typeof Shell === 'function' ? Shell : null;", {
    exports: exported, require: (name) => imports[name] ?? {},
  });
  return { exported, navigations };
}

function findRoute(node, path, ancestors = []) {
  if (!node || typeof node !== "object") return null;
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findRoute(child, path, ancestors);
      if (found) return found;
    }
    return null;
  }
  const next = node.type === "Route" ? [...ancestors, node] : ancestors;
  if (node.type === "Route" && node.props.path === path) return next;
  return findRoute(node.props?.children, path, next);
}

test("members retain both core workflows in their sidebar", () => {
  const { exported } = renderModule("../App.tsx", { role: "member", token: "session" });
  const shell = exported.Shell();
  const nav = shell.props.children.find((child) => child?.props?.nav).props.nav;
  const links = nav.flatMap((group) => group.items.map((item) => item.to));
  assert.ok(links.includes("/automation"));
  assert.ok(links.includes("/warehouse"));
});

test("a signed-in member can open automation without a role redirect", () => {
  const { exported } = renderModule("../App.tsx", { role: "member", token: "session" });
  const route = findRoute(exported.default(), "automation");
  assert.ok(route, "automation route must exist");
  for (const ancestor of route.slice(0, -1)) {
    const element = ancestor.props.element;
    if (typeof element?.type !== "function") continue;
    const rendered = element.type(element.props);
    assert.notEqual(rendered?.type, "Navigate", "member must reach the automation page");
  }
});

test("automation still requires software login", () => {
  const { exported } = renderModule("../App.tsx", null);
  const route = findRoute(exported.default(), "automation");
  assert.ok(route);
  const loginRedirect = route.slice(0, -1).some((ancestor) => {
    const element = ancestor.props.element;
    if (typeof element?.type !== "function") return false;
    const rendered = element.type(element.props);
    return rendered?.type === "Navigate" && rendered.props.to === "/login";
  });
  assert.equal(loginRedirect, true);
});

test("member login returns to the requested automation workflow", () => {
  for (const from of ["/automation", "/collect", "/collect-box", "/images"]) {
    const { exported, navigations } = renderModule(
      "../features/auth/LoginPage.tsx", { role: "member", token: "session" }, from,
    );
    exported.LoginPage();
    assert.equal(navigations[0], from);
  }
});
