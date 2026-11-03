import assert from "node:assert/strict";
import test from "node:test";
import { collectBoxListQueryKey } from "./queryKeys.ts";

test("collect box sort variants use different query keys", () => {
  const shared = {
    accountId: "account-1",
    page: 1,
    pageSize: 20,
    status: "notPublished",
    keyword: "",
    shopId: null,
  };

  const defaultSortKey = collectBoxListQueryKey(shared);
  const explicitDefaultSortKey = collectBoxListQueryKey({ ...shared, sort: "default" });
  const savedDateSortKey = collectBoxListQueryKey({ ...shared, sort: "savedDesc" });

  assert.notDeepEqual(defaultSortKey, savedDateSortKey);
  assert.deepEqual(defaultSortKey, explicitDefaultSortKey);
  assert.equal(defaultSortKey[1].sort, "default");
  assert.equal(savedDateSortKey[1].sort, "savedDesc");
});
