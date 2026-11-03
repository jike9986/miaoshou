import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient } from "@tanstack/react-query";
import {
  clearAllOperatorQueryDiskCaches,
  clearOperatorQueryDiskCache,
  hydrateOperatorQueryClient,
  persistOperatorQueryClient,
} from "./operatorQueryDiskCache.mjs";

class MemoryStorage {
  values = new Map();

  get length() { return this.values.size; }
  key(index) { return [...this.values.keys()][index] ?? null; }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

function useStorage(t) {
  const originalWindow = globalThis.window;
  globalThis.window = { localStorage: new MemoryStorage() };
  t.after(() => {
    clearAllOperatorQueryDiskCaches();
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  });
  return globalThis.window.localStorage;
}

test("disk cache is scoped by software user and MiaoShou account, and omits details", (t) => {
  const storage = useStorage(t);
  const source = new QueryClient();
  source.setQueryData(["collect-box", { accountId: "miaoshou-a", page: 1 }], {
    items: [{ title: "account A" }],
  });
  source.setQueryData(["collect-box", { accountId: "miaoshou-b", page: 1 }], {
    items: [{ title: "account B" }],
  });
  source.setQueryData(["collect-box", "miaoshou-a", "detail-id", "shop-id"], {
    payload: "detail should stay in memory only",
  });
  const unsubscribe = persistOperatorQueryClient("user-a:member", source);

  assert.equal(storage.length, 2);
  const userAAccountA = new QueryClient();
  hydrateOperatorQueryClient("user-a:member", userAAccountA);
  assert.deepEqual(
    userAAccountA.getQueryData(["collect-box", { accountId: "miaoshou-a", page: 1 }]),
    { items: [{ title: "account A" }] },
  );
  assert.deepEqual(
    userAAccountA.getQueryData(["collect-box", { accountId: "miaoshou-b", page: 1 }]),
    { items: [{ title: "account B" }] },
  );
  assert.equal(userAAccountA.getQueryData(["collect-box", "miaoshou-a", "detail-id", "shop-id"]), undefined);

  const userB = new QueryClient();
  hydrateOperatorQueryClient("user-b:member", userB);
  assert.equal(userB.getQueryCache().getAll().length, 0);
  unsubscribe();
});

test("identity without a stable software user id is not persisted", (t) => {
  const storage = useStorage(t);
  const client = new QueryClient();
  client.setQueryData(["warehouse", { accountId: "miaoshou-a", page: 1 }], {
    items: [{ title: "private" }],
  });

  const unsubscribe = persistOperatorQueryClient(null, client);

  assert.equal(storage.length, 0);
  unsubscribe();
});

test("same user and MiaoShou account do not restore cache from another server", (t) => {
  const storage = useStorage(t);
  const serverA = new QueryClient();
  serverA.setQueryData(["collect-box", { accountId: "miaoshou-a", page: 1 }], {
    items: [{ title: "server A data" }],
  });
  const unsubscribe = persistOperatorQueryClient(
    "user-a:member:https://server-a.example",
    serverA,
  );

  const serverB = new QueryClient();
  hydrateOperatorQueryClient("user-a:member:https://server-b.example", serverB);

  assert.equal(storage.length, 1);
  assert.equal(serverB.getQueryCache().getAll().length, 0);
  unsubscribe();
});

test("reading prunes expired pages only for the current user's disk cache", (t) => {
  const storage = useStorage(t);
  const now = Date.now();
  const expiredAt = now - 8 * 24 * 60 * 60 * 1000;
  const currentUser = new QueryClient();
  currentUser.setQueryData(["collect-box", { accountId: "miaoshou-a", page: 1 }],
    { items: ["old page"] }, { updatedAt: expiredAt });
  currentUser.setQueryData(["collect-box", { accountId: "miaoshou-a", page: 2 }],
    { items: ["fresh page"] }, { updatedAt: now });
  currentUser.setQueryData(["warehouse", { accountId: "expired-account", page: 1 }],
    { items: ["expired account"] }, { updatedAt: expiredAt });
  const stopCurrent = persistOperatorQueryClient("current-user:member", currentUser);
  const otherUser = new QueryClient();
  otherUser.setQueryData(["warehouse", { accountId: "miaoshou-a", page: 1 }],
    { items: ["another user's old cache"] }, { updatedAt: expiredAt });
  const stopOther = persistOperatorQueryClient("other-user:member", otherUser);
  const otherKey = [...storage.values.keys()].find((key) => key.includes("other-user"));
  const otherRaw = storage.getItem(otherKey);

  const restored = new QueryClient();
  hydrateOperatorQueryClient("current-user:member", restored);

  assert.equal(restored.getQueryData(["collect-box", { accountId: "miaoshou-a", page: 1 }]), undefined);
  assert.deepEqual(restored.getQueryData(["collect-box", { accountId: "miaoshou-a", page: 2 }]),
    { items: ["fresh page"] });
  assert.equal(restored.getQueryData(["warehouse", { accountId: "expired-account", page: 1 }]), undefined);
  assert.equal(storage.length, 2);
  assert.equal(storage.getItem(otherKey), otherRaw);
  const currentKey = [...storage.values.keys()].find((key) => key.includes("current-user"));
  assert.equal(JSON.parse(storage.getItem(currentKey)).entries.length, 1);
  stopCurrent();
  stopOther();
});

test("one malformed cache does not hide fresh lists or clear another user's cache", (t) => {
  const storage = useStorage(t);
  const client = new QueryClient();
  client.setQueryData(["collect-box", { accountId: "miaoshou-a", page: 1 }], { items: ["valid"] });
  const stop = persistOperatorQueryClient("current-user:member", client);
  const badKey = "aohuang.query-cache.v2.current-user%3Amember.account.broken";
  const otherKey = "aohuang.query-cache.v2.other-user%3Amember.account.broken";
  storage.setItem(badKey, "{broken");
  storage.setItem(otherKey, "{broken");
  storage.setItem("aohuang.operator.token", "keep-login");

  const restored = new QueryClient();
  hydrateOperatorQueryClient("current-user:member", restored);

  assert.deepEqual(restored.getQueryData(["collect-box", { accountId: "miaoshou-a", page: 1 }]),
    { items: ["valid"] });
  assert.equal(storage.getItem(badKey), null);
  assert.equal(storage.getItem(otherKey), "{broken");
  assert.equal(storage.getItem("aohuang.operator.token"), "keep-login");
  stop();
});

test("cache reads remain usable when expiry cleanup cannot write to local storage", (t) => {
  const storage = useStorage(t);
  const client = new QueryClient();
  client.setQueryData(["warehouse", { accountId: "miaoshou-a", page: 1 }],
    { items: ["fresh"] });
  client.setQueryData(["warehouse", { accountId: "miaoshou-a", page: 2 }],
    { items: ["expired"] }, { updatedAt: 1 });
  const stop = persistOperatorQueryClient("current-user:member", client);
  storage.setItem = () => { throw new Error("storage quota"); };

  const restored = new QueryClient();
  hydrateOperatorQueryClient("current-user:member", restored);

  assert.deepEqual(restored.getQueryData(["warehouse", { accountId: "miaoshou-a", page: 1 }]),
    { items: ["fresh"] });
  assert.equal(restored.getQueryData(["warehouse", { accountId: "miaoshou-a", page: 2 }]), undefined);
  stop();
});

test("manual cache clearing reports storage failures", (t) => {
  const storage = useStorage(t);
  storage.setItem("aohuang.query-cache.v2.current-user%3Amember.account.a", "cached");
  storage.removeItem = () => { throw new Error("storage denied"); };

  assert.equal(clearOperatorQueryDiskCache("current-user:member"), false);
});
