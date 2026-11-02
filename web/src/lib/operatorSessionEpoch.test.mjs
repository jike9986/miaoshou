import assert from "node:assert/strict";
import test from "node:test";
import {
  advanceOperatorSessionEpoch,
  getOperatorSessionEpoch,
  runIfOperatorSessionCurrent,
} from "./operatorSessionEpoch.mjs";

test("late mutation response cannot write into a new operator's shared account cache", () => {
  const cache = new Map([["batch:shared-account:batch-1", "operator-b"]]);
  const startedByA = getOperatorSessionEpoch();

  advanceOperatorSessionEpoch();
  const wrote = runIfOperatorSessionCurrent(startedByA, () => {
    cache.set("batch:shared-account:batch-1", "late response from operator-a");
  });

  assert.equal(wrote, false);
  assert.equal(cache.get("batch:shared-account:batch-1"), "operator-b");
});

test("current operator mutation response may update its cache", () => {
  const cache = new Map();
  const startedByCurrentOperator = getOperatorSessionEpoch();

  const wrote = runIfOperatorSessionCurrent(startedByCurrentOperator, () => {
    cache.set("batch:shared-account:batch-1", "current response");
  });

  assert.equal(wrote, true);
  assert.equal(cache.get("batch:shared-account:batch-1"), "current response");
});
