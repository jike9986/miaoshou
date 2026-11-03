import fs from "node:fs";
import path from "node:path";
import type { SafeStorage } from "electron";

/** Desktop sessions stay in the main process and use the current OS user's encryption. */
export function createSessionVault(
  file: string,
  scope: () => string,
  encryption: Pick<SafeStorage, "isEncryptionAvailable" | "encryptString" | "decryptString">,
) {
  let loadedScope: string | undefined;
  let token: string | null = null;

  function read(): string | null {
    const currentScope = scope();
    if (loadedScope === currentScope) return token;
    loadedScope = currentScope;
    token = null;
    try {
      const saved = JSON.parse(fs.readFileSync(file, "utf8")) as {
        version?: number; scope?: string; encrypted?: string;
      };
      if (saved.version === 1 && saved.scope === currentScope && saved.encrypted
        && encryption.isEncryptionAvailable()) {
        token = encryption.decryptString(Buffer.from(saved.encrypted, "base64")) || null;
      }
    } catch {
      // A copied profile, damaged file, or changed Windows user requires a fresh login.
    }
    return token;
  }

  function write(value: string | null): boolean {
    loadedScope = scope();
    token = value;
    // No plaintext fallback: without OS encryption the session lasts only until exit.
    let encrypted: string | null = null;
    try {
      if (value && encryption.isEncryptionAvailable()) {
        encrypted = encryption.encryptString(value).toString("base64");
      }
    } catch {
      // An unavailable OS key must never cause a plaintext fallback.
    }
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(`${file}.tmp`, JSON.stringify({
        version: 1, scope: loadedScope, encrypted,
      }), { mode: 0o600 });
      fs.renameSync(`${file}.tmp`, file);
      return value === null || encrypted !== null;
    } catch {
      // A failed replacement must not restore the previous user's session on restart.
      try {
        fs.unlinkSync(file);
        return value === null;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") return value === null;
      }
      try {
        fs.writeFileSync(file, JSON.stringify({ version: 1, scope: loadedScope, encrypted: null }), { mode: 0o600 });
        return value === null;
      } catch {
        // The caller must report a logout that could not clear the persisted session.
      }
      return false;
    }
  }

  return { read, write };
}
