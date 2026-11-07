#!/usr/bin/env node
// Validate the actual release payload, including startup with legacy environment flags.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL, URL } from "node:url";

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
const synchronousHandlers = new Map();
const navigationHandlers = new Map();
let loadedFile;
let startup;
let windowOptions;
let window;
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
  constructor(options) {
    window = this;
    windowOptions = options;
    this.webContents = { mainFrame: { url: "about:blank" },
      setWindowOpenHandler() {}, on: (event, handler) => navigationHandlers.set(event, handler) };
  }
  once() {}
  loadFile(file, options) {
    loadedFile = { file, options };
    this.webContents.mainFrame.url = pathToFileURL(file).href;
    return Promise.resolve();
  }
}
const nativeRequire = createRequire(import.meta.url);
const context = {
  require(name) {
    if (name === "electron") return {
      app, BrowserWindow,
      ipcMain: { handle: (key, handler) => handlers.set(key, handler),
        on: (key, handler) => synchronousHandlers.set(key, handler) },
      screen: { getPrimaryDisplay: () => ({ workAreaSize: { width: 853, height: 480 }, workArea: { x: 0, y: 24 } }) },
      shell: { openExternal() {} },
      safeStorage: { isEncryptionAvailable: () => false },
    };
    if (name === "./sessionVault") {
      const module = { exports: {} };
      vm.runInNewContext(asar.extractFile(archive, "dist/sessionVault.js").toString(), {
        exports: module.exports, require: nativeRequire, Buffer,
      }, { filename: "packaged-sessionVault.js" });
      return module.exports;
    }
    if (name === "./updateInstaller") {
      // 自更新模块要用 net / createWriteStream / spawn，这些在打包校验里
      // 不该真跑（校验只验「模块能加载、IPC 能注册」，不验下载）。
      // 注意 spawn 在 node:child_process 的 stub 里会抛错（防本地后端），
      // 所以这里单独给一个不抛的版本，否则模块加载阶段就被打断。
      const module = { exports: {} };
      vm.runInNewContext(asar.extractFile(archive, "dist/updateInstaller.js").toString(), {
        exports: module.exports,
        require(name2) {
          if (name2 === "electron") return {
            app: { getPath: () => testData },
            BrowserWindow: { getAllWindows: () => [] },
            net: { request: () => ({ on() {}, end() {} }) },
          };
          if (name2 === "node:child_process") {
            return { spawn: () => ({ unref() {} }) };
          }
          if (name2 === "node:fs/promises" || name2 === "node:fs"
              || name2 === "node:crypto" || name2 === "node:path") {
            return nativeRequire(name2);
          }
          return nativeRequire(name2);
        },
        Buffer,
        process,
        URL,
        setTimeout,
      }, { filename: "packaged-updateInstaller.js" });
      return module.exports;
    }
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
  URL,
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
const sessionEvent = () => {
  let writes = 0;
  let result;
  return {
    sender: window.webContents, senderFrame: window.webContents.mainFrame,
    get returnValue() { return result; },
    set returnValue(value) {
      assert.equal(writes++, 0, "Synchronous IPC must send exactly one reply");
      result = value;
    },
  };
};
let event = sessionEvent();
synchronousHandlers.get("session:write")(event, "package-verification-only");
event = sessionEvent();
synchronousHandlers.get("session:read")(event);
assert.equal(event.returnValue, "package-verification-only");
const trustedUrl = window.webContents.mainFrame.url;
for (const url of ["https://example.invalid/", "data:text/html,untrusted", pathToFileURL(path.join(resources, "web", "other.html")).href]) {
  window.webContents.mainFrame.url = url;
  event = sessionEvent();
  synchronousHandlers.get("session:read")(event);
  assert.equal(event.returnValue, null, "Untrusted page could read a session");
  event = sessionEvent();
  synchronousHandlers.get("session:write")(event, "untrusted");
  assert.equal(event.returnValue, false, "Untrusted page could replace a session");
  let prevented = false;
  navigationHandlers.get("will-navigate")({ preventDefault() { prevented = true; } }, url);
  assert.equal(prevented, true, "Untrusted navigation was allowed");
}
window.webContents.mainFrame.url = trustedUrl;
event = sessionEvent();
synchronousHandlers.get("session:write")(event, null);
assert.equal(event.returnValue, true, "Trusted logout could not clear its session");
console.log(`Desktop package verified: v${boot.version}, ${checkedFiles} current web files, HTTPS software login, no shared token or local backend, small work area respected.`);
