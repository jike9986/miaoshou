import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createSessionVault } from "../dist/sessionVault.js";

const directory = fs.mkdtempSync(fileURLToPath(new URL("../../_build_tmp/session-vault-", import.meta.url)));
function encryption(key = crypto.randomBytes(32)) {
  return {
    isEncryptionAvailable: () => true,
    encryptString(value) {
      const iv = crypto.randomBytes(12);
      const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
      const encrypted = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
      return Buffer.concat([iv, cipher.getAuthTag(), encrypted]);
    },
    decryptString(value) {
      const decipher = crypto.createDecipheriv("aes-256-gcm", key, value.subarray(0, 12));
      decipher.setAuthTag(value.subarray(12, 28));
      return decipher.update(value.subarray(28), undefined, "utf8") + decipher.final("utf8");
    },
  };
}

test("a persisted session is encrypted and survives restart for the same server", () => {
  const file = path.join(directory, "restart.json");
  const key = encryption();
  assert.equal(createSessionVault(file, () => "https://test.invalid", key).write("private-token"), true);
  assert.equal(fs.readFileSync(file, "utf8").includes("private-token"), false);
  assert.equal(createSessionVault(file, () => "https://test.invalid", key).read(), "private-token");
  assert.equal(createSessionVault(file, () => "https://other.invalid", key).read(), null);
  assert.equal(createSessionVault(file, () => "https://test.invalid", encryption()).read(), null);
});

test("logout removes the persisted token and cannot restore it after restart", () => {
  const file = path.join(directory, "logout.json");
  const key = encryption();
  const vault = createSessionVault(file, () => "https://test.invalid", key);
  vault.write("previous-token");
  assert.equal(vault.write(null), true);
  assert.equal(vault.read(), null);
  assert.equal(createSessionVault(file, () => "https://test.invalid", key).read(), null);
});

test("failed atomic replacement clears the old session before restart", () => {
  for (const value of [null, "replacement-session"]) {
    const file = path.join(directory, value === null ? "blocked-logout.json" : "blocked-login.json");
    const key = encryption();
    const vault = createSessionVault(file, () => "test", key);
    assert.equal(vault.write("previous-session"), true);
    fs.mkdirSync(`${file}.tmp`);
    assert.equal(vault.write(value), value === null);
    assert.equal(vault.read(), value);
    assert.equal(createSessionVault(file, () => "test", key).read(), null);
  }
});

test("encryption unavailable or throwing keeps only the new in-memory session", () => {
  for (const variant of ["unavailable", "throwing"]) {
    const file = path.join(directory, `${variant}.json`);
    const key = encryption();
    createSessionVault(file, () => "test", key).write("old-token");
    const unavailable = { ...key, isEncryptionAvailable: () => variant !== "unavailable",
      encryptString: () => { throw new Error("OS key unavailable"); } };
    const vault = createSessionVault(file, () => "test", unavailable);
    assert.equal(vault.write("new-token"), false);
    assert.equal(vault.read(), "new-token");
    assert.equal(fs.readFileSync(file, "utf8").includes("new-token"), false);
    assert.equal(createSessionVault(file, () => "test", key).read(), null);
  }
});

test("corrupt data and unwritable profile do not crash login", () => {
  const file = path.join(directory, "damaged.json");
  fs.writeFileSync(file, "{broken");
  assert.equal(createSessionVault(file, () => "test", encryption()).read(), null);
  const vault = createSessionVault(path.join(file, "not-a-directory"), () => "test", encryption());
  assert.equal(vault.write("memory-only"), false);
  assert.equal(vault.read(), "memory-only");
});
