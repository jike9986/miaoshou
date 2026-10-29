import test from "node:test";
import assert from "node:assert/strict";
import { normalizeApiBase } from "./serverAddress.ts";

test("normalizes HTTPS origins and removes a trailing slash", () => {
  assert.equal(normalizeApiBase(" https://api.example.com:8443/ "), "https://api.example.com:8443");
});

test("allows HTTP only for loopback addresses", () => {
  assert.equal(normalizeApiBase("http://localhost:8812"), "http://localhost:8812");
  assert.equal(normalizeApiBase("http://127.12.0.3:8812"), "http://127.12.0.3:8812");
  assert.equal(normalizeApiBase("http://[::1]:8812"), "http://[::1]:8812");
  assert.throws(() => normalizeApiBase("http://192.168.1.8:8812"), /HTTPS/);
});

test("rejects credentials, paths, query strings, fragments, and other protocols", () => {
  for (const value of [
    "https://user:pass@example.com",
    "https://example.com/api",
    "https://example.com?tenant=1",
    "https://example.com#fragment",
    "ftp://example.com",
  ]) {
    assert.throws(() => normalizeApiBase(value));
  }
});
