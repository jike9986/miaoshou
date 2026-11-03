import assert from "node:assert/strict";
import test from "node:test";
import { consumeOneShotRefreshFlag } from "./oneShotRefreshFlag.mjs";

test("force refresh is consumed once and does not affect later automatic reads", () => {
  const flag = { current: false };

  flag.current = true;
  assert.equal(consumeOneShotRefreshFlag(flag), true);
  assert.equal(consumeOneShotRefreshFlag(flag), false);
  assert.equal(flag.current, false);
});
