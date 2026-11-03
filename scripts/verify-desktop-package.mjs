#!/usr/bin/env node
// Validate the actual release payload, including startup with legacy environment flags.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const unpacked = path.join(root, "release", "win-unpacked");
const resources = path.join(unpacked, "resources");
const builderRequire = createRequire(fs.realpathSync(path.join(root, "app", "node_modules", "electron-builder", "package.json")));
const libRequire = createRequire(builderRequire.resolve("app-builder-lib"));
const asar = libRequire("@electron/asar");
const archive = path.join(resources, "app.asar");
const packaged = JSON.parse(asar.extractFile(archive, "package.json").toString());
const expected = JSON.parse(fs.readFileSync(path.join(root, "app", "package.json"), "utf8"));
assert.equal(packaged.version, expected.version, "Packaged app version is stale");
assert.ok(fs.existsSync(path.join(unpacked, "AohuangShangpin.exe")), "Executable is missing");
assert.ok(!fs.existsSync(path.join(resources, "client-token.json")), "Legacy shared token must not ship");
assert.ok(!fs.existsSync(path.join(resources, "server")), "Remote client must not ship a local backend");

const web = path.join(root, "web", "dist");
let checkedFiles = 0;
function compareWeb(directory, relative = "") {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const name = path.join(relative, entry.name);
    if (entry.isDirectory()) compareWeb(path.join(directory, entry.name), name);
    else {
      assert.ok(fs.readFileSync(path.join(web, name)).equals(fs.readFileSync(path.join(resources, "web", name))), `Stale web file: ${name}`);
      checkedFiles += 1;
    }
  }
}
compareWeb(web);

const handlers = new Map();
let loadedFile;
let startup;
let windowOptions;
const testData = path.join(root, "_build_tmp", "package-verification-profile");
const app = {
  isPackaged: true,
  commandLine: { appendSwitch() {} },
  getVersion: () => packaged.version,
  getPath: (key) => key === "exe" ? path.join(unpacked, "AohuangShangpin.exe") : testData,
  setPath() {},
  on() {},
  whenReady: () => ({ then(callback) { startup = Promise.resolve().then(callback); return startup; } }),
};
class BrowserWindow {
  constructor(options) { windowOptions = options; this.webContents = { setWindowOpenHandler() {} }; }
  once() {}
  loadFile(file, options) { loadedFile = { file, options }; return Promise.resolve(); }
}
const nativeRequire = createRequire(import.meta.url);
const context = {
  require(name) {
    if (name === "electron") return {
      app, BrowserWindow,
      ipcMain: { handle: (key, handler) => handlers.set(key, handler), on() {} },
      screen: { getPrimaryDisplay: () => ({ workAreaSize: { width: 853, height: 480 }, workArea: { x: 0, y: 24 } }) },
      shell: {},
    };
    if (name === "node:child_process") return { spawn() { throw new Error("Packaged client started a local backend"); } };
    return nativeRequire(name);
  },
  exports: {},
  __dirname: path.join(resources, "app.asar", "dist"),
  process: { platform: "win32", resourcesPath: resources, env: {
    AOHUANG_DATA_DIR: testData,
    AOHUANG_LOCAL_BACKEND: "1",
    AOHUANG_REMOTE_CLIENT_TOKEN: "legacy-test-token-must-be-ignored",
  } },
  console,
  setTimeout,
};
vm.runInNewContext(asar.extractFile(archive, "dist/main.js").toString(), context, { filename: "packaged-main.js" });
await startup;
const boot = handlers.get("app:boot")();
assert.equal(boot.apiBase, "https://aohuang888.com/miaoshou-platform");
assert.equal(boot.token, "", "Legacy environment token bypassed software login");
assert.equal(boot.version, expected.version);
assert.equal(loadedFile.file, path.join(resources, "web", "index.html"));
assert.equal(loadedFile.options.query.apiBase, boot.apiBase);
assert.equal(windowOptions.width, 853);
assert.equal(windowOptions.height, 480);
assert.ok(windowOptions.minWidth <= 853 && windowOptions.minHeight <= 480);
assert.equal(windowOptions.x, 0);
assert.equal(windowOptions.y, 24);
console.log(`Desktop package verified: v${boot.version}, ${checkedFiles} current web files, HTTPS software login, no shared token or local backend, small work area respected.`);
