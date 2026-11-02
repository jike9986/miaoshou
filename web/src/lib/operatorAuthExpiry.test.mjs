import assert from "node:assert/strict";
import test from "node:test";
import { shouldClearOperatorSession } from "./operatorAuthExpiry.mjs";

test("a 401 clears only the operator session that issued the request", () => {
  assert.equal(shouldClearOperatorSession(401, "expired-token", "expired-token"), true);
  assert.equal(shouldClearOperatorSession(401, "old-token", "new-token"), false);
  assert.equal(shouldClearOperatorSession(401, "expired-token", null), false);
  assert.equal(shouldClearOperatorSession(403, "token", "token"), false);
  assert.equal(shouldClearOperatorSession(401, null, "current-token"), false);
});
