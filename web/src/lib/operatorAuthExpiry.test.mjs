import assert from "node:assert/strict";
import test from "node:test";
import {
  isPublicAuthRequest,
  requestSessionToken,
  shouldClearOperatorSession,
} from "./operatorAuthExpiry.mjs";

test("a 401 clears only the operator session that issued the request", () => {
  assert.equal(shouldClearOperatorSession(401, "expired-token", "expired-token"), true);
  assert.equal(shouldClearOperatorSession(401, "old-token", "new-token"), false);
  assert.equal(shouldClearOperatorSession(401, "expired-token", null), false);
  assert.equal(shouldClearOperatorSession(403, "token", "token"), false);
  assert.equal(shouldClearOperatorSession(401, null, "current-token"), false);
});

test("email-code login endpoints are public and cannot invalidate an open session", () => {
  for (const path of [
    "/auth/email-code-login/request",
    "/auth/email-code-login/confirm",
  ]) {
    assert.equal(isPublicAuthRequest("POST", path), true);
    assert.equal(requestSessionToken("POST", path, "current-token", undefined), null);
  }
});

test("first-password setup remains private and associates 401 with its session", () => {
  const path = "/auth/complete-password-setup";
  assert.equal(isPublicAuthRequest("POST", path), false);
  assert.equal(requestSessionToken("POST", path, "current-token", undefined), "current-token");
});

test("incorrect current password is recoverable while an invalid session still expires", () => {
  const token = requestSessionToken("POST", "/auth/change-password", "current-token", undefined);
  assert.equal(token, "current-token");
  assert.equal(shouldClearOperatorSession(400, token, "current-token"), false);
  assert.equal(shouldClearOperatorSession(401, token, "current-token"), true);
});

test("public auth access is restricted to exact method and path pairs", () => {
  assert.equal(isPublicAuthRequest("GET", "/auth/credentials-status"), true);
  assert.equal(isPublicAuthRequest("POST", "/auth/credentials-status"), false);
  assert.equal(isPublicAuthRequest("GET", "/auth/email-code-login/request"), false);
  assert.equal(isPublicAuthRequest("POST", "/auth/email-code-login/request/extra"), false);
});
