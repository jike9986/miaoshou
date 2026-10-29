import assert from "node:assert/strict";
import test from "node:test";
import { buildQualityReviewRequest } from "./qualityReviewInput.ts";

const values = {
  weightKg: "0.42",
  packageLength: "18.5",
  packageWidth: "12",
  packageHeight: "7.25",
  source: " 妙手商品资料 ",
  evidence: " 商品详情页包装参数已核对 ",
};

test("normalizes entered values and preserves the reviewed source", () => {
  assert.deepEqual(buildQualityReviewRequest(values, 3), {
    request: {
      weightKg: 0.42,
      packageLength: 18.5,
      packageWidth: 12,
      packageHeight: 7.25,
      source: "妙手商品资料",
      evidence: "商品详情页包装参数已核对",
      expectedRevision: 3,
    },
  });
});

test("rejects empty, non-finite, out-of-range, and untraceable inputs", () => {
  assert.match(buildQualityReviewRequest({ ...values, weightKg: "" }, 3).error, /有效数字/);
  assert.match(buildQualityReviewRequest({ ...values, packageWidth: "NaN" }, 3).error, /有效数字/);
  assert.match(buildQualityReviewRequest({ ...values, weightKg: "1001" }, 3).error, /不能超过/);
  assert.match(buildQualityReviewRequest({ ...values, source: "  " }, 3).error, /核验来源/);
  assert.match(buildQualityReviewRequest({ ...values, evidence: " x " }, 3).error, /核验依据/);
  assert.match(buildQualityReviewRequest(values, 0).error, /版本号/);
});
