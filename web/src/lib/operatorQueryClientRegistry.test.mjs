import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient } from "@tanstack/react-query";
import {
  clearOperatorProductListCache,
  clearOperatorQueryClients,
  clearOperatorQueryClientsExcept,
  getOperatorQueryClient,
} from "./operatorQueryClientRegistry.mjs";

test("switching directly between operators reuses isolated query caches", () => {
  const root = new QueryClient({ defaultOptions: { queries: { staleTime: 1234 } } });
  const operatorA = getOperatorQueryClient("operator-a:member", root);
  operatorA.setQueryData(["warehouse"], { owner: "operator-a" });

  const operatorB = getOperatorQueryClient("operator-b:member", root);
  assert.notEqual(operatorB, operatorA);
  assert.equal(operatorB.getQueryData(["warehouse"]), undefined);
  assert.equal(operatorB.getDefaultOptions().queries?.staleTime, 1234);

  operatorB.setQueryData(["warehouse"], { owner: "operator-b" });
  assert.equal(getOperatorQueryClient("operator-a:member", root), operatorA);
  assert.deepEqual(operatorA.getQueryData(["warehouse"]), { owner: "operator-a" });
  assert.deepEqual(operatorB.getQueryData(["warehouse"]), { owner: "operator-b" });

  clearOperatorQueryClients();
});

test("legacy identities without ids still keep token-scoped caches isolated", () => {
  const root = new QueryClient();
  const legacyA = getOperatorQueryClient("legacy-token-a:", root);
  legacyA.setQueryData(["warehouse"], { owner: "legacy-a" });

  const legacyB = getOperatorQueryClient("legacy-token-b:", root);
  assert.notEqual(legacyB, legacyA);
  assert.equal(legacyB.getQueryData(["warehouse"]), undefined);

  legacyB.setQueryData(["warehouse"], { owner: "legacy-b" });
  assert.deepEqual(legacyA.getQueryData(["warehouse"]), { owner: "legacy-a" });
  assert.deepEqual(legacyB.getQueryData(["warehouse"]), { owner: "legacy-b" });

  clearOperatorQueryClients();
});

test("changing an operator role does not reuse the previous role cache", () => {
  const root = new QueryClient();
  const teacher = getOperatorQueryClient("same-operator:teacher", root);
  teacher.setQueryData(["operators"], [{ name: "private student list" }]);

  const member = getOperatorQueryClient("same-operator:member", root);

  assert.notEqual(member, teacher);
  assert.equal(member.getQueryData(["operators"]), undefined);
  assert.deepEqual(teacher.getQueryData(["operators"]), [
    { name: "private student list" },
  ]);

  clearOperatorQueryClients();
});

test("changing API base does not reuse another server's in-memory cache", () => {
  const root = new QueryClient();
  const serverA = getOperatorQueryClient("same-user:member:https://server-a.example", root);
  serverA.setQueryData(["warehouse"], { source: "server-a" });

  const serverB = getOperatorQueryClient("same-user:member:https://server-b.example", root);

  assert.notEqual(serverB, serverA);
  assert.equal(serverB.getQueryData(["warehouse"]), undefined);
  assert.deepEqual(serverA.getQueryData(["warehouse"]), { source: "server-a" });

  clearOperatorQueryClients();
});

test("leaving an operator clears its sensitive cache and retains the public cache", () => {
  const root = new QueryClient();
  const operatorA = getOperatorQueryClient("operator-a:member", root);
  operatorA.setQueryData(["publish-logs"], [{ title: "private product" }]);
  const publicClient = getOperatorQueryClient(null, root);
  publicClient.setQueryData(["software-login"], { available: true });

  clearOperatorQueryClientsExcept(publicClient);

  assert.equal(operatorA.getQueryData(["publish-logs"]), undefined);
  assert.deepEqual(publicClient.getQueryData(["software-login"]), { available: true });
  assert.equal(getOperatorQueryClient(null, root), publicClient);

  clearOperatorQueryClients();
});

test("clearing the current product previews preserves login, details, and other users", async (t) => {
  const originalWindow = globalThis.window;
  const values = new Map();
  const localStorage = {
    get length() { return values.size; },
    key: (index) => [...values.keys()][index] ?? null,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
  globalThis.window = { localStorage };
  t.after(() => {
    clearOperatorQueryClients();
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  });
  localStorage.setItem("aohuang.operator.token", "keep-login");
  const root = new QueryClient();
  const current = getOperatorQueryClient("user-a", root, "user-a:member:server-a");
  const listKey = ["collect-box", { accountId: "shared-key", page: 1 }];
  current.setQueryData(listKey, { items: ["current-user"] });
  current.setQueryData(["warehouse", { accountId: "shared-key", page: 1 }], { items: ["warehouse"] });
  current.setQueryData(["collect-box", "shared-key", "detail-id", "shop-id"], { title: "detail" });
  current.setQueryData(["auth", "status"], { accountId: "shared-key" });
  const other = getOperatorQueryClient("user-b", root, "user-b:member:server-a");
  other.setQueryData(listKey, { items: ["other-user"] });
  let finishRequest;
  let aborted = false;
  const request = new Promise((resolve) => { finishRequest = resolve; });
  const pending = current.fetchQuery({ queryKey: listKey, queryFn: ({ signal }) => {
    signal.addEventListener("abort", () => { aborted = true; });
    return request;
  } }).catch((error) => error);

  await clearOperatorProductListCache(current);
  finishRequest({ items: ["late response"] });
  await pending;
  assert.equal(aborted, true);

  assert.equal(current.getQueryData(listKey), undefined);
  assert.equal(current.getQueryData(["warehouse", { accountId: "shared-key", page: 1 }]), undefined);
  assert.deepEqual(current.getQueryData(["collect-box", "shared-key", "detail-id", "shop-id"]), { title: "detail" });
  assert.deepEqual(current.getQueryData(["auth", "status"]), { accountId: "shared-key" });
  assert.deepEqual(other.getQueryData(listKey), { items: ["other-user"] });
  assert.equal(localStorage.getItem("aohuang.operator.token"), "keep-login");
  assert.equal([...values.keys()].filter((key) => key.startsWith("aohuang.query-cache.v2.")).length, 1);
  assert.equal([...values.keys()].some((key) => key.includes("user-a%3A")), false);
});
