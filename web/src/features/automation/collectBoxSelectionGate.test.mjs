import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { isCollectBoxReadOnly } from "./collectBoxSelectionGate.mjs";

test("cached collect-box rows remain read-only until this view has verified them", () => {
  for (const state of [
    { loading: true },
    { verified: false },
    { placeholder: true },
    { degraded: true },
    { failed: true },
  ]) {
    assert.equal(isCollectBoxReadOnly({ verified: true, ...state }), true);
  }
  assert.equal(isCollectBoxReadOnly({ verified: true }), false);
});

function isReadOnly(result) {
  return isCollectBoxReadOnly({
    loading: result.isLoading,
    verified: result.isFetchedAfterMount,
    placeholder: result.isPlaceholderData,
    degraded: result.data?.degraded,
    failed: result.isError || result.isRefetchError,
  });
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}

test("first cached verification locks selection but later polling does not", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const first = deferred();
  const poll = deferred();
  let request = first;
  const observer = new QueryObserver(client, {
    queryKey: ["collect-box", "account-a", 1],
    queryFn: () => request.promise,
    initialData: { items: [{ key: "shop:1" }] },
  });
  const unsubscribe = observer.subscribe(() => {});
  try {
    assert.equal(observer.getCurrentResult().isFetching, true);
    assert.equal(isReadOnly(observer.getCurrentResult()), true);
    first.resolve({ items: [{ key: "shop:1" }] });
    await first.promise;
    await Promise.resolve();
    assert.equal(isReadOnly(observer.getCurrentResult()), false);

    request = poll;
    const refreshed = observer.refetch();
    assert.equal(observer.getCurrentResult().isFetching, true);
    assert.equal(isReadOnly(observer.getCurrentResult()), false);
    poll.resolve({ items: [{ key: "shop:2" }, { key: "shop:1" }] });
    await refreshed;
    assert.equal(isReadOnly(observer.getCurrentResult()), false);

    request = deferred();
    const failed = observer.refetch();
    request.reject(new Error("upstream unavailable"));
    await failed;
    assert.equal(isReadOnly(observer.getCurrentResult()), true);
  } finally {
    unsubscribe();
    client.clear();
  }
});

test("switching to another cached account requires its own verification", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const aKey = ["collect-box", "account-a", 1];
  const bKey = ["collect-box", "account-b", 1];
  client.setQueryData(aKey, { items: [{ key: "shop:a" }] });
  client.setQueryData(bKey, { items: [{ key: "shop:b" }] });
  const bRequest = deferred();
  const observer = new QueryObserver(client, {
    queryKey: aKey,
    queryFn: async () => ({ items: [{ key: "shop:a" }] }),
  });
  const unsubscribe = observer.subscribe(() => {});
  try {
    await observer.refetch();
    assert.equal(isReadOnly(observer.getCurrentResult()), false);
    observer.setOptions({ queryKey: bKey, queryFn: () => bRequest.promise });
    assert.equal(observer.getCurrentResult().data.items[0].key, "shop:b");
    assert.equal(isReadOnly(observer.getCurrentResult()), true);
    bRequest.resolve({ items: [{ key: "shop:b" }] });
    await bRequest.promise;
    await Promise.resolve();
    assert.equal(isReadOnly(observer.getCurrentResult()), false);
  } finally {
    unsubscribe();
    client.clear();
  }
});
