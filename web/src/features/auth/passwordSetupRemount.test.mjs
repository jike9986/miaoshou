import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { URL } from "node:url";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import * as authFormHelpers from "./authFormHelpers.mjs";
import * as emailBinding from "./emailBinding.mjs";

const require = createRequire(import.meta.url);
const componentSource = ts.transpileModule(
  readFileSync(new URL("./UnlockPage.tsx", import.meta.url), "utf8"),
  { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } },
).outputText;
const settingsSource = ts.transpileModule(
  readFileSync(new URL("../settings/OperatorsPanel.tsx", import.meta.url), "utf8"),
  { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } },
).outputText + "\nexports.ChangeSoftwarePasswordDialog = ChangeSoftwarePasswordDialog;";

// Exercise the actual panel and event handlers while replacing browser and API
// boundaries. Resetting hook state models the identity provider's keyed remount.
function mountPanel(initialIdentity = null, componentName = "SoftwareAccountPanel") {
  let identity = initialIdentity;
  let state = [];
  let cursor = 0;
  let id = 0;
  const calls = [];
  const navigations = [];
  const mutations = new Map();
  const queries = new Proxy({}, {
    get: (_, hook) => () => {
      if (hook === "useOperators") return {
        data: { operators: identity ? [identity] : [], current: identity },
        isLoading: false,
        isError: false,
      };
      if (!mutations.has(hook)) mutations.set(hook, {
        isPending: false,
        mutate: (body, callbacks) => calls.push({ hook, body, callbacks }),
      });
      return mutations.get(hook);
    },
  });
  const imports = {
    react: {
      useState: (initial) => {
        const index = cursor++;
        if (!(index in state)) state[index] = typeof initial === "function" ? initial() : initial;
        return [state[index], (next) => {
          state[index] = typeof next === "function" ? next(state[index]) : next;
        }];
      },
      useId: () => `field-${id++}`,
      useRef: () => ({ current: null }),
      useEffect: () => {},
      useMemo: (value) => value(),
    },
    "react/jsx-runtime": require("react/jsx-runtime"),
    "react-router-dom": { useNavigate: () => (...args) => navigations.push(args) },
    "@tanstack/react-query": { useQueryClient: () => ({ invalidateQueries: () => {} }) },
    "lucide-react": new Proxy({}, { get: (_, icon) => icon }),
    "framer-motion": {},
    "@/lib/motion": {},
    "@/lib/utils": { cn: (...values) => values.filter(Boolean).join(" ") },
    "@/lib/api": { apiErrorMessage: (error) => error.message },
    "@/lib/queries": queries,
    "@/lib/operator": {
      useOperatorIdentity: () => identity,
      currentOperator: () => identity,
      setOperator: (next) => { identity = next; },
      LOCAL_OPERATOR_NAME: "Local",
    },
    "@/features/stores": {},
    "@/features/auth/emailBinding.mjs": emailBinding,
    "@/features/auth/authFormHelpers.mjs": authFormHelpers,
    "@/features/auth/useEmailCodeCooldown": {
      useEmailCodeCooldown: () => ({ remainingSeconds: 0, startCooldown: () => {} }),
    },
    "@/components/ui": new Proxy({}, {
      get: (_, component) => component === "toast" ? { success: () => {} } : component,
    }),
    "@/components/layout": {},
    "@/lib/queryKeys": {},
    "@/features/settings/OneTimeTokenDialog": {
      OneTimeTokenDialog: "OneTimeTokenDialog",
      ONE_TIME_TOKEN_NOTICE: "",
    },
  };
  const exported = {};
  runInNewContext(componentName === "SoftwareAccountPanel" ? componentSource : settingsSource, {
    exports: exported,
    require: (name) => {
      assert.ok(name in imports, `Unexpected component import: ${name}`);
      return imports[name];
    },
    window: { requestAnimationFrame: (callback) => callback() },
  });
  function render() {
    cursor = 0;
    id = 0;
    return exported[componentName]({ redirectTo: "/warehouse", onBack: () => {}, onClose: () => {} });
  }
  return {
    render,
    remount: () => { state = []; return render(); },
    calls,
    navigations,
    mutations,
    identity: () => identity,
    replaceIdentity: (next) => { identity = next; },
  };
}

function nodes(tree) {
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  if (!tree || typeof tree !== "object") return [];
  return [tree, ...nodes(tree.props?.children)];
}

function click(tree, label) {
  const button = nodes(tree).find((node) =>
    ["Button", "button"].includes(node.type) && node.props.children === label);
  assert.ok(button, `Missing button: ${label}`);
  button.props.onClick();
}

function fill(tree, label, value) {
  const field = nodes(tree).find((node) => node.type === "Field" && node.props.label === label);
  assert.ok(field, `Missing field: ${label}`);
  const input = nodes(field).find((node) => node.type === "Input");
  input.props.onChange({ currentTarget: { value } });
}

const forcedIdentity = {
  token: "first-session", id: "learner-id", name: "Learner", role: "member", mustChangePassword: true,
};

test("email-code first login survives the operator provider remount and sets a password", () => {
  const panel = mountPanel();
  click(panel.render(), "邮箱验证码");
  fill(panel.render(), "邮箱", "learner@example.com");
  click(panel.render(), "发送登录验证码");
  panel.calls.at(-1).callbacks.onSuccess();
  fill(panel.render(), "邮箱验证码", "123456");
  click(panel.render(), "验证并登录");
  panel.calls.at(-1).callbacks.onSuccess({
    token: forcedIdentity.token,
    operator: { id: forcedIdentity.id, name: forcedIdentity.name, role: forcedIdentity.role },
    mustChangePassword: true,
  });

  const restored = panel.remount();
  assert.deepEqual(nodes(restored).filter((node) => node.type === "Field").map((node) => node.props.label), ["新密码"]);
  fill(restored, "新密码", "new-password-123");
  click(panel.render(), "保存新密码");
  const setup = panel.calls.at(-1);
  assert.equal(setup.hook, "useCompletePasswordSetup");
  assert.equal(JSON.stringify(setup.body), JSON.stringify({ newPassword: "new-password-123" }));
  assert.equal(panel.identity().mustChangePassword, true);
  setup.callbacks.onSuccess({ token: "rotated-session", mustChangePassword: false });
  assert.equal(panel.identity().token, "rotated-session");
  assert.equal(panel.identity().mustChangePassword, false);
  assert.equal(panel.navigations[0][0], "/warehouse");
});

test("cold reload of a forced session permits only new-password setup and keeps server errors", () => {
  const panel = mountPanel(forcedIdentity);
  const restored = panel.render();
  assert.equal(nodes(restored).some((node) => node.type === "Input" && node.props.autoComplete === "current-password"), false);
  fill(restored, "新密码", "new-password-123");
  click(panel.render(), "保存新密码");
  panel.calls.at(-1).callbacks.onError(new Error("Session expired"));
  assert.equal(panel.identity().mustChangePassword, true);
  assert.equal(panel.identity().token, forcedIdentity.token);
  assert.equal(panel.navigations.length, 0);
  assert.equal(nodes(panel.render()).some((node) => node.type === "InlineError" && node.props.children === "Session expired"), true);
});

test("ordinary sessions do not expose first-password setup and pending setup does not resubmit", () => {
  const regular = mountPanel({ ...forcedIdentity, mustChangePassword: false });
  assert.equal(nodes(regular.render()).some((node) => node.type === "Input" && node.props.autoComplete === "new-password"), false);
  assert.equal(regular.calls.length, 0);

  const forced = mountPanel(forcedIdentity);
  fill(forced.render(), "新密码", "new-password-123");
  forced.mutations.get("useCompletePasswordSetup").isPending = true;
  click(forced.render(), "保存新密码");
  assert.equal(forced.calls.length, 0);
});

test("member and teacher account settings both expose the self-service password dialog", () => {
  for (const role of ["member", "teacher"]) {
    const panel = mountPanel({ ...forcedIdentity, role, mustChangePassword: false }, "OperatorsPanel");
    click(panel.render(), "修改密码");
    assert.equal(nodes(panel.render()).some((node) => node.type?.name === "ChangeSoftwarePasswordDialog"), true);
  }
});

test("self-service password change validates confirmation and preserves the session on rejection", () => {
  const identity = { ...forcedIdentity, mustChangePassword: false };
  const panel = mountPanel(identity, "ChangeSoftwarePasswordDialog");
  fill(panel.render(), "当前密码", "current-pass-123");
  fill(panel.render(), "新密码", "new-password-123");
  const submit = () => nodes(panel.render()).find((node) => node.type === "form")
    .props.onSubmit({ preventDefault() {} });
  submit();
  assert.equal(panel.calls.length, 0);
  fill(panel.render(), "确认新密码", "new-password-123");
  submit();
  const change = panel.calls.at(-1);
  assert.equal(change.hook, "useChangeSoftwarePassword");
  assert.equal(JSON.stringify(change.body), JSON.stringify({
    currentPassword: "current-pass-123", newPassword: "new-password-123",
  }));
  change.callbacks.onError(new Error("当前密码错误"));
  assert.equal(panel.identity(), identity);
  assert.equal(nodes(panel.render()).some((node) => node.type === "InlineError" && node.props.children === "当前密码错误"), true);
  change.callbacks.onSuccess({ token: "updated-session", mustChangePassword: false });
  assert.equal(panel.identity().token, "updated-session");
});

test("a late password response cannot replace another signed-in user's session", () => {
  const panel = mountPanel({ ...forcedIdentity, mustChangePassword: false }, "ChangeSoftwarePasswordDialog");
  fill(panel.render(), "当前密码", "current-pass-123");
  fill(panel.render(), "新密码", "new-password-123");
  fill(panel.render(), "确认新密码", "new-password-123");
  nodes(panel.render()).find((node) => node.type === "form").props.onSubmit({ preventDefault() {} });
  const nextIdentity = { ...forcedIdentity, id: "other-user", token: "other-session" };
  panel.replaceIdentity(nextIdentity);
  panel.calls.at(-1).callbacks.onSuccess({ token: "late-session", mustChangePassword: false });
  assert.equal(panel.identity(), nextIdentity);
});
