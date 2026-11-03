import test from "node:test";
import assert from "node:assert/strict";
import { allowsLocalBootstrapToken, shouldShowLocalTokenNotice } from "./apiTarget.ts";

test("remote software login does not require a local bootstrap token", () => {
  for (const hasSoftwareSession of [false, true]) {
    assert.equal(shouldShowLocalTokenNotice({
      apiBase: "https://aohuang888.com/miaoshou-platform",
      hasRuntimeToken: false,
      hasSoftwareSession,
    }), false);
  }
});

test("a software session also satisfies authentication on a local backend", () => {
  assert.equal(shouldShowLocalTokenNotice({
    apiBase: "http://127.0.0.1:8812",
    hasRuntimeToken: false,
    hasSoftwareSession: true,
  }), false);
});

test("local bootstrap failures remain visible until either token is available", () => {
  for (const apiBase of ["", "http://127.0.0.1:8812", "http://localhost:8812", "http://[::1]:8812"]) {
    assert.equal(shouldShowLocalTokenNotice({
      apiBase, hasRuntimeToken: false, hasSoftwareSession: false,
    }), true);
    assert.equal(shouldShowLocalTokenNotice({
      apiBase, hasRuntimeToken: true, hasSoftwareSession: false,
    }), false);
  }
});

test("allows bootstrap only for the effective trusted loopback target", () => {
  assert.equal(allowsLocalBootstrapToken({}), true);
  assert.equal(allowsLocalBootstrapToken({ environmentBase: "http://127.0.0.1:8812" }), true);
  assert.equal(allowsLocalBootstrapToken({ bootBase: "http://localhost:8812" }), true);
  assert.equal(allowsLocalBootstrapToken({
    bootBase: "http://localhost:8812/",
    queryBase: "http://localhost:8812",
  }), true);
});

test("never sends a local bootstrap token when a higher-priority target is remote", () => {
  assert.equal(allowsLocalBootstrapToken({
    environmentBase: "http://127.0.0.1:8812",
    bootBase: "https://api.example.com",
  }), false);
  assert.equal(allowsLocalBootstrapToken({
    environmentBase: "http://127.0.0.1:8812",
    queryBase: "https://api.example.com",
  }), false);
  assert.equal(allowsLocalBootstrapToken({
    bootBase: "https://api.example.com",
    queryBase: "https://other.example.com",
  }), false);
  assert.equal(allowsLocalBootstrapToken({ environmentBase: "https://api.example.com" }), false);
});

test("explicit saved and legacy targets do not receive the local bootstrap token", () => {
  assert.equal(allowsLocalBootstrapToken({
    savedBase: "http://localhost:8812",
    environmentBase: "http://localhost:8812",
  }), false);
  assert.equal(allowsLocalBootstrapToken({ legacyBase: "http://localhost:8812" }), false);
  assert.equal(allowsLocalBootstrapToken({ queryBase: "http://localhost:8812" }), false);
});
