import assert from "node:assert/strict";
import test from "node:test";
import {
  EMAIL_CODE_RESEND_COOLDOWN_SECONDS,
  normalizeCooldownSeconds,
  remainingCooldownSeconds,
  retryAfterSecondsFromError,
  validateSoftwareLogin,
} from "./authFormHelpers.mjs";

test("software login validation reports missing identifier and password per field", () => {
  assert.deepEqual(validateSoftwareLogin("  ", ""), {
    identifier: "请输入邮箱或用户名",
    password: "请输入密码",
  });
  assert.deepEqual(validateSoftwareLogin(" learner@example.com ", "secret"), {
    identifier: null,
    password: null,
  });
  assert.deepEqual(validateSoftwareLogin("learner", " "), {
    identifier: null,
    password: null,
  });
});

test("email code success uses a short client-side resend cooldown", () => {
  assert.equal(EMAIL_CODE_RESEND_COOLDOWN_SECONDS, 60);
});

test("rate-limit cooldown honors only a positive server retryAfterSeconds", () => {
  assert.equal(retryAfterSecondsFromError({
    status: 429,
    details: { retryAfterSeconds: 17 },
  }), 17);
  assert.equal(retryAfterSecondsFromError({
    status: 429,
    details: { retryAfterSeconds: 1.2 },
  }), 2);
  assert.equal(retryAfterSecondsFromError({
    status: 500,
    details: { retryAfterSeconds: 17 },
  }), 0);
  assert.equal(retryAfterSecondsFromError({
    status: 429,
    details: { retryAfterSeconds: -1 },
  }), 0);
});

test("cooldown helpers round up remaining time and treat elapsed deadlines as ready", () => {
  assert.equal(normalizeCooldownSeconds("3.1"), 4);
  assert.equal(normalizeCooldownSeconds(Number.NaN), 0);
  assert.equal(remainingCooldownSeconds(12_500, 10_000), 3);
  assert.equal(remainingCooldownSeconds(9_000, 10_000), 0);
});
