import assert from "node:assert/strict";
import test from "node:test";
import { canUseCollectBoxPlaceholder } from "./collectBoxPlaceholder.mjs";

const current = {
  accountId: "miaoshou-a",
  page: 1,
  pageSize: 20,
  status: "notPublished",
  keyword: "",
  shopId: null,
  sort: "default",
};

test("collect-box placeholder is rejected when page or server filter changes", () => {
  for (const change of [
    { page: 2 },
    { pageSize: 50 },
    { status: "published" },
    { keyword: "shirt" },
    { shopId: "shop-b" },
    { sort: "savedDesc" },
    { accountId: "miaoshou-b" },
  ]) {
    assert.equal(canUseCollectBoxPlaceholder(current, { ...current, ...change }), false);
  }
});

test("collect-box placeholder permits only the same request scope", () => {
  assert.equal(canUseCollectBoxPlaceholder(current, { ...current }), true);
});
