import assert from "node:assert/strict";
import test from "node:test";
import { withAuthorizationHeader } from "./requestAuth.ts";

test("an explicit target identity token overrides the current identity token", () => {
  const headers = withAuthorizationHeader(
    { Accept: "application/json", Authorization: "Bearer manually-supplied-token" },
    "current-operator-token",
    "target-operator-token",
  );

  assert.equal(headers.Authorization, "Bearer target-operator-token");
});

test("requests without an explicit identity continue using the default token", () => {
  const headers = withAuthorizationHeader(
    { Authorization: "Bearer manually-supplied-token" },
    "current-operator-token",
  );

  assert.equal(headers.Authorization, "Bearer current-operator-token");
});

test("public requests without a token omit the Authorization header", () => {
  const headers = withAuthorizationHeader({ Accept: "application/json" }, null);

  assert.equal(headers.Authorization, undefined);
});
