import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = ts.transpileModule(readFileSync(new URL("./operator.ts", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText;

function load({ desktop = true, initial = null, failVault = false, failStorage = false } = {}) {
  const storage = new Map(initial ? [["aohuang.operator.token", initial]] : []);
  const events = new Map();
  let protectedToken = null;
  const bridge = {
    read() { if (failVault) throw new Error("bridge unavailable"); return protectedToken; },
    write(value) { if (failVault) throw new Error("bridge unavailable"); protectedToken = value; return true; },
  };
  const exports = {};
  let subscribe;
  runInNewContext(source, {
    exports,
    require(name) {
      if (name === "react") return { useSyncExternalStore: (fn) => { subscribe = fn; } };
      return { advanceOperatorSessionEpoch() {} };
    },
    window: {
      desktop: desktop ? { sessionToken: bridge } : undefined,
      localStorage: {
        getItem: (key) => { if (failStorage) throw new Error("storage unavailable"); return storage.get(key) ?? null; },
        setItem: (key, value) => storage.set(key, value),
        removeItem: (key) => storage.delete(key),
      },
      addEventListener: (event, callback) => events.set(event, callback),
      removeEventListener: (event) => events.delete(event),
    },
  });
  return { api: exports, storage, protectedToken: () => protectedToken,
    subscribe: () => { exports.useOperatorIdentity(); return subscribe(() => {}); }, events };
}

test("desktop upgrades migrate the legacy token and remove its plaintext copy", () => {
  const { api, storage, protectedToken } = load({ initial: "legacy-private-token" });
  assert.equal(api.operatorToken(), "legacy-private-token");
  assert.equal(protectedToken(), "legacy-private-token");
  assert.equal(storage.has(api.OPERATOR_TOKEN_KEY), false);
  api.setOperator({ token: "new-private-token", id: "learner", name: "Learner", role: "member" });
  assert.equal(api.currentOperator().id, "learner");
  assert.equal(api.operatorToken(), "new-private-token");
  assert.equal(protectedToken(), "new-private-token");
  assert.equal(storage.has(api.OPERATOR_TOKEN_KEY), false);
  api.clearOperator();
  assert.equal(api.operatorToken(), null);
  assert.equal(protectedToken(), null);
});

test("desktop bridge failure keeps the session in memory without plaintext persistence", () => {
  const { api, storage } = load({ initial: "legacy", failVault: true });
  assert.equal(api.operatorToken(), "legacy");
  assert.equal(storage.has(api.OPERATOR_TOKEN_KEY), false);
  api.setOperator({ token: "active-session", id: "learner", name: "Learner" });
  assert.equal(api.operatorToken(), "active-session");
  assert.equal(storage.has(api.OPERATOR_TOKEN_KEY), false);
  api.clearOperator();
  assert.equal(api.operatorToken(), null);
});

test("browser sessions still follow cross-window token changes and logout", () => {
  const { api, storage, subscribe, events } = load({ desktop: false });
  api.setOperator({ token: "first", name: "First" });
  assert.equal(storage.get(api.OPERATOR_TOKEN_KEY), "first");
  const unsubscribe = subscribe();
  storage.set(api.OPERATOR_TOKEN_KEY, "other-window");
  events.get("storage")({ key: api.OPERATOR_TOKEN_KEY });
  assert.equal(api.operatorToken(), "other-window");
  storage.delete(api.OPERATOR_TOKEN_KEY);
  events.get("storage")({ key: api.OPERATOR_TOKEN_KEY });
  assert.equal(api.operatorToken(), null);
  unsubscribe();
});

test("disabled browser storage preserves an active session in memory", () => {
  const { api } = load({ desktop: false, failStorage: true });
  api.setOperator({ token: "active", name: "Learner" });
  assert.equal(api.operatorToken(), "active");
  api.clearOperator();
  assert.equal(api.operatorToken(), null);
});
