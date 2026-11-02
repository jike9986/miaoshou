import assert from "node:assert/strict";
import test from "node:test";
import { publishLogsPlaceholderData } from "./publishLogsPlaceholder.mjs";

const previousRows = { items: [{ id: "publish-log-a" }] };

test("publish logs from another MiaoShou account are not used as placeholder data", () => {
  const previousQuery = {
    queryKey: ["publish-logs", { accountId: "account-a", page: 1 }],
  };

  assert.equal(publishLogsPlaceholderData(previousRows, previousQuery, "account-b"), undefined);
});

test("publish logs remain as placeholder data when paging within the same account", () => {
  const previousQuery = {
    queryKey: ["publish-logs", { accountId: "account-a", page: 1 }],
  };

  assert.equal(publishLogsPlaceholderData(previousRows, previousQuery, "account-a"), previousRows);
});

test("anonymous publish logs can remain placeholders for the anonymous account", () => {
  const previousQuery = {
    queryKey: ["publish-logs", { accountId: null, page: 1 }],
  };

  assert.equal(publishLogsPlaceholderData(previousRows, previousQuery, null), previousRows);
});
