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

// 安装器参数回归（2026-11-23 事故）。
//
// 事故原样：客户端给 **Inno** 打的安装包传 NSIS 的 `/S` → Inno 不认 → 弹图形向导
// → 用户关掉软件后什么也没发生，下次点快捷方式还是老版本。
//
// 现在安装包改成 electron-builder 自带的 **NSIS**（大厂标准路径），
// 参数必须是 NSIS 的 `/S`，并且 `/D=` 钉住安装目录。这里扫打包产物把这两条钉死。
const installerCode = asar.extractFile(archive, "dist/updateDownload.js").toString();
const installerCaller = asar.extractFile(archive, "dist/updateInstaller.js").toString();
/** 去掉 `//` / `/*` / `*` 注释行 —— 注释里写历史踩坑不算违规，真代码里才有害。 */
const codeLines = (text) => text
  .split("\n")
  .filter((line) => {
    const trimmed = line.trim();
    return !trimmed.startsWith("//") && !trimmed.startsWith("/*") && !trimmed.startsWith("*");
  })
  .join("\n");
assert.ok(codeLines(installerCode).includes('"/S"'),
  "Packaged client must ask the NSIS installer for a silent install (/S)");
assert.ok(codeLines(installerCode).includes("/D="),
  "Packaged client must pin the install directory (/D=), otherwise the installer falls back to %LOCALAPPDATA% and the desktop shortcut keeps launching the old version");
assert.ok(installerCaller.includes("installerArguments"),
  "Packaged client must build installer args through installerArguments()");
assert.ok(!codeLines(installerCode).includes("/VERYSILENT"),
  "NSIS installers do not accept Inno's /VERYSILENT");
assert.ok(!codeLines(installerCaller).includes("'/S'"),
  "installer args must come from updateDownload.installerArguments(), not be hardcoded");
// 安装包必须在自己被运行之后才允许被删掉：老代码 `await discardInstaller()`
// 紧跟在不等待的 PowerShell spawn 之后，把 900ms 后才执行的 setup.exe 删了 ——
// 静默升级永远不生效（这正是「关掉软件什么也没发生」的根因）。
// 只扫真正的代码行（注释里说明这件事的文字不算）。
assert.ok(!codeLines(installerCaller).includes("await discardInstaller()"),
  "installOnQuit must not delete the installer before PowerShell runs it");

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
      setWindowOpenHandler() {}, on: (event, handler) => navigationHandlers.set(event, handler),
      getZoomFactor: () => 1.0, setZoomFactor() {},
      // main.ts:createWindow 会调它把捏合缩放固定成 1（Electron 31 的 API）。
      // mock 少了这个方法，脚本会抛 TypeError 而不是报告真正的产物问题。
      setVisualZoomLevelLimits() {} };
  }
  once() {}
  on() {}
  getContentSize() { return [windowOptions.width, windowOptions.height]; }
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
    if (name === "./updateDownload") {
      // 纯逻辑模块（白名单校验 + 切片规划），不依赖 electron，
      // 直接用 node 原生 require 跑 asar 里编译好的那份。
      const module = { exports: {} };
      vm.runInNewContext(asar.extractFile(archive, "dist/updateDownload.js").toString(), {
        exports: module.exports,
        require: nativeRequire,
        Buffer,
        URL,
      }, { filename: "packaged-updateDownload.js" });
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
          if (name2 === "./updateDownload") {
            // updateInstaller 依赖 updateDownload 的白名单与切片规划
            const sub = { exports: {} };
            vm.runInNewContext(asar.extractFile(archive, "dist/updateDownload.js").toString(), {
              exports: sub.exports,
              require: nativeRequire,
              Buffer,
              URL,
            }, { filename: "packaged-updateDownload.js" });
            return sub.exports;
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
