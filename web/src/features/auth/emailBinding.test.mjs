import assert from "node:assert/strict";
import test from "node:test";
import { normalizeEmailForVerification } from "./emailBinding.mjs";

test("trims and normalizes a valid mailbox before verification requests", () => {
  assert.equal(normalizeEmailForVerification("  Learner.Name+shop@Example.COM "), "learner.name+shop@example.com");
});

test("rejects malformed mailbox values accepted by neither request nor confirm", () => {
  for (const value of ["", "not-an-email", "a@b", "a@@example.com", "a..b@example.com", "a@-example.com"]) {
    assert.equal(normalizeEmailForVerification(value), null, value);
  }
});

test("rejects consecutive dots and malformed domain labels before auth requests", () => {
  for (const value of ["a..b@example.com", "a@example..com", "a@-example.com", "a@example-.com"]) {
    assert.equal(normalizeEmailForVerification(value), null, value);
  }
});

test("rejects oversized local parts and addresses", () => {
  assert.equal(normalizeEmailForVerification(`${"a".repeat(65)}@example.com`), null);
  assert.equal(normalizeEmailForVerification(`${"a".repeat(246)}@example.com`), null);
});
