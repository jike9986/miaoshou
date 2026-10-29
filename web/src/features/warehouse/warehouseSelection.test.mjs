import assert from "node:assert/strict";
import test from "node:test";
import { partitionMemberSelection } from "./warehouseSelection.ts";

test("keeps claim and publish actions for selections made on earlier pages", () => {
  const selectedIds = ["page-1-unclaimed", "page-1-claimed", "page-2-unclaimed"];
  const claimedById = {
    "page-1-unclaimed": false,
    "page-1-claimed": true,
    "page-2-unclaimed": false,
  };

  assert.deepEqual(partitionMemberSelection(selectedIds, claimedById), {
    claimable: ["page-1-unclaimed", "page-2-unclaimed"],
    publishable: ["page-1-claimed"],
  });
});

test("does not include selections whose status snapshot was cleared", () => {
  assert.deepEqual(partitionMemberSelection(["stale-selection"], {}), {
    claimable: [],
    publishable: [],
  });
});

test("hides items already published to the selected shop but keeps other shops eligible", () => {
  assert.deepEqual(partitionMemberSelection(
    ["published-here", "published-elsewhere"],
    { "published-here": true, "published-elsewhere": true },
    { "published-here": ["shop-a"], "published-elsewhere": ["shop-b"] },
    "shop-a",
  ), {
    claimable: [],
    publishable: ["published-elsewhere"],
  });
});
