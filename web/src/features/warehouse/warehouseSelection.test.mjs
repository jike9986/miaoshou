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
