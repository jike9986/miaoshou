import assert from "node:assert/strict";
import test from "node:test";
import {
  partitionMemberSelection,
  selectedQualityReviews,
  warehouseIdsForClaimedDetails,
} from "./warehouseSelection.ts";

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

test("keeps verified quality snapshots for selected products across pages", () => {
  assert.deepEqual(selectedQualityReviews(
    ["verified-on-page-1", "unverified-on-page-2"],
    {
      "verified-on-page-1": { status: "verified", reasons: [] },
      "unverified-on-page-2": { status: "needs_review", reasons: ["missing_weight"] },
    },
  ), {
    "verified-on-page-1": { status: "verified", reasons: [] },
    "unverified-on-page-2": { status: "needs_review", reasons: ["missing_weight"] },
  });
});

test("treats a missing quality snapshot as unknown rather than verified", () => {
  assert.deepEqual(selectedQualityReviews(["unknown-quality"], {}), {
    "unknown-quality": { status: undefined, reasons: [] },
  });
});

test("maps successful claim detail IDs back to selected warehouse IDs", () => {
  assert.deepEqual(warehouseIdsForClaimedDetails(
    ["warehouse-1", "warehouse-2", "warehouse-3"],
    { "warehouse-1": "detail-1", "warehouse-2": "detail-2", "warehouse-3": "detail-3" },
    ["detail-1", "detail-3"],
  ), ["warehouse-1", "warehouse-3"]);
});
