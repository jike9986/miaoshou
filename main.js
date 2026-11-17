"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
const electron_1 = require("electron");
const node_fs_1 = __importDefault(require("node:fs"));
const node_path_1 = __importDefault(require("node:path"));
const node_os_1 = __importDefault(require("node:os"));
const node_url_1 = require("node:url");
const node_child_process_1 = require("node:child_process");
const sessionVault_1 = require("./sessionVault");
const updateInstaller_1 = require("./updateInstaller");
// 本机实测结论（决定性实验）：
//   在 m.js 第一行写日志，然后无参数启动 electron —— 日志文件根本不生成。
//   说明崩溃发生在**加载 JS 之前**的原生初始化阶段，不是我们的代码。
//   只加 --user-data-dir        → 仍然崩（所以 userData 不是原因）
//   只加 --no-sandbox           → 正常到 ready
// 也就是说 appendSwitch 在这里只是给「开发态 pnpm dev」兜底，
// 真正让 exe 起来的是命令行上的 --no-sandbox（由 launch.bat / 快捷方式提供）。
electron_1.app.commandLine.appendSwitch("no-sandbox");
electron_1.app.commandLine.appendSwitch("disable-gpu-sandbox");
// userData 仍需指到可写位置：本机 %APPDATA% 下写不进去时，
// 即便沙箱问题已解决，Chromium 初始化 profile 也会失败（另一条崩溃路径）。
// 逐个候选尝试，第一个可写的就用。
{
    const candidates = [
        process.env.AOHUANG_DATA_DIR && node_path_1.default.join(process.env.AOHUANG_DATA_DIR, "electron"),
        process.env.LOCALAPPDATA && node_path_1.default.join(process.env.LOCALAPPDATA, "aohuang-desktop", "electron"),
        node_path_1.default.join(node_os_1.default.tmpdir(), "aohuang-desktop-electron"),
        node_path_1.default.join(node_path_1.default.dirname(electron_1.app.getPath("exe")), "userdata"),
    ].filter((item) => Boolean(item));
    for (const dir of candidates) {
        try {
            node_fs_1.default.mkdirSync(dir, { recursive: true });
            node_fs_1.default.accessSync(dir, node_fs_1.default.constants.W_OK);
            electron_1.app.setPath("userData", dir);
            break;
        }
        catch {
            /* 换下一个候选 */
        }
    }
}
const node_http_1 = __importDefault(require("node:http"));
const DEV_URL = "http://127.0.0.1:5173";
// 后端会从 API_PORT 起向上找一个没被占用的端口（见 server/main.py 的 pick_port）。
// 所以这里不能写死 8812：必须启动后探测实际端口，再把它交给前端。
const API_PORT = 8812;
// 与后端 core/config.py 的 api_port_max 保持一致（默认 8822），否则后端换到
// 8816+ 时 Electron 探不到，窗口会报接口不可用。
const API_PORT_MAX = 8822;
const isDev = !electron_1.app.isPackaged;
let mainWindow = null;
/** 实际交给后端的数据目录；loadToken 必须从这里读 secret.key，两处不能各算各的 */
let dataDir = "";
let server = null;
/** 后端实际监听的端口；启动探测成功后写入，供 bootInfo / 窗口 query 使用 */
let apiPort = API_PORT;
function ping(url, timeout = 800) {
    return new Promise((resolve) => {
        const req = node_http_1.default.get(url, (res) => {
            res.resume();
            resolve((res.statusCode ?? 0) < 500);
        });
        req.on("error", () => resolve(false));
        req.setTimeout(timeout, () => {
            req.destroy();
            resolve(false);
        });
    });
}
/** 在 API_PORT..API_PORT_MAX 里找出真正在响应 /api/v1/system/health 的端口 */
async function detectApiPort() {
    for (let port = API_PORT; port <= API_PORT_MAX; port += 1) {
        if (await ping(`http://127.0.0.1:${port}/api/v1/system/health`)) {
            return port;
        }
    }
    // 都探不到时退回首选端口，由调用方按失败处理
    return API_PORT;
}
/**
 * 选一个**确实可写**的数据目录给后端。
 * 本机 %LOCALAPPDATA% 对派生进程不可写，直接用后端的默认值会让它退出码 3。
 * 优先级：exe 同级 userdata（可整体拷走）→ Electron userData → 临时目录。
 */
function pickDataDir() {
    const candidates = [
        node_path_1.default.join(node_path_1.default.dirname(process.execPath), "userdata"),
        electron_1.app.getPath("userData"),
        node_path_1.default.join(node_os_1.default.tmpdir(), "aohuang-desktop"),
    ];
    for (const dir of candidates) {
        try {
            node_fs_1.default.mkdirSync(dir, { recursive: true });
            // 真写一次才算数：mkdir 成功不代表能建文件
            const probe = node_path_1.default.join(dir, ".write-probe");
            node_fs_1.default.writeFileSync(probe, "ok");
            node_fs_1.default.unlinkSync(probe);
            return dir;
        }
        catch {
            /* 换下一个 */
        }
    }
    return candidates[candidates.length - 1];
}
function startServer() {
    if (isDev) {
        server = (0, node_child_process_1.spawn)("python", ["-m", "uvicorn", "main:app", "--port", String(API_PORT)], {
            cwd: node_path_1.default.join(__dirname, "..", "..", "server"),
            stdio: "inherit",
            windowsHide: true,
        });
        return;
    }
    // 打包后运行 PyInstaller 生成的后端可执行文件（不需要用户装 Python）
    const bundled = node_path_1.default.join(process.resourcesPath, "server", "aohuang-server.exe");
    // 数据目录必须显式传给后端，且必须是**可写**的位置。
    // 后端默认落在 %LOCALAPPDATA%\aohuang-desktop，本机派生进程写不进去：
    //   PermissionError: [Errno 13] ... \logs\server.log
    //   sqlite3.OperationalError: unable to open database file
    //   Application startup failed. Exiting.     ← 退出码 3
    // 结果就是「窗口闪一下没了」。这里优先用 exe 同级的 userdata（一定可写，
    // 也方便用户整体拷走），拿不到才退回 Electron 自己的 userData。
    dataDir = pickDataDir();
    // 后端输出定向到文件，不要用 stdio:"ignore"：本机派生进程碰管道容易出问题，
    // 落盘既能跑起来，也方便事后查后端为什么退出。
    const logDir = node_path_1.default.join(dataDir, "logs");
    let out = "ignore";
    try {
        node_fs_1.default.mkdirSync(logDir, { recursive: true });
        out = node_fs_1.default.openSync(node_path_1.default.join(logDir, "server.out.log"), "a");
    }
    catch {
        out = "ignore";
    }
    server = (0, node_child_process_1.spawn)(bundled, [], {
        stdio: ["ignore", out, out],
        windowsHide: true,
        env: {
            ...process.env,
            AOHUANG_API_PORT: String(API_PORT),
            AOHUANG_DATA_DIR: dataDir,
        },
    });
    server.on("error", (err) => {
        console.error("后端启动失败:", err.message);
    });
    server.on("exit", (code, signal) => {
        if (code !== 0 && code !== null) {
            console.error(`后端退出 code=${code} signal=${signal}`);
        }
    });
}
function isTrustedPage(value) {
    try {
        const url = new URL(value);
        if (isDev)
            return url.origin === DEV_URL && url.pathname === "/";
        return url.protocol === "file:" && !url.host
            && node_path_1.default.resolve((0, node_url_1.fileURLToPath)(url)).toLowerCase()
                === node_path_1.default.resolve(process.resourcesPath, "web", "index.html").toLowerCase();
    }
    catch {
        return false;
    }
}
function openWebLink(value) {
    try {
        const url = new URL(value);
        if (url.protocol === "https:" || url.protocol === "http:")
            void electron_1.shell.openExternal(value);
    }
    catch { /* Ignore malformed external links. */ }
}
function createWindow() {
    // Electron 使用逻辑像素；高缩放或小屏幕下不能把窗口最小尺寸固定得比工作区还大。
    const display = electron_1.screen.getPrimaryDisplay();
    const workArea = display.workAreaSize;
    const width = Math.min(1440, workArea.width);
    const height = Math.min(920, workArea.height);
    mainWindow = new electron_1.BrowserWindow({
        width,
        height,
        minWidth: Math.min(900, workArea.width),
        minHeight: Math.min(600, workArea.height),
        x: display.workArea.x + Math.floor((workArea.width - width) / 2),
        y: display.workArea.y + Math.floor((workArea.height - height) / 2),
        show: false,
        frame: false,
        titleBarStyle: "hidden",
        backgroundColor: "#F6F8F9",
        webPreferences: {
            preload: node_path_1.default.join(__dirname, "preload.js"),
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: false,
        },
    });
    mainWindow.once("ready-to-show", () => mainWindow?.show());
    // 根据窗口宽度自动缩放，解决大屏下 UI 比例偏小的问题。
    // 用 setZoomFactor（Electron 级缩放）而非 CSS zoom：前者让 100vh/媒体查询等
    // 都按缩放后的逻辑像素计算，不会出现内容溢出屏幕底部的情况。
    function getTargetZoom() {
        if (!mainWindow)
            return 1.0;
        const [w] = mainWindow.getContentSize();
        if (w >= 1800)
            return 1.15;
        if (w >= 1600)
            return 1.08;
        return 1.0;
    }
    function applyZoom() {
        if (!mainWindow)
            return;
        const target = getTargetZoom();
        const current = mainWindow.webContents.getZoomFactor();
        if (Math.abs(current - target) > 0.01) {
            mainWindow.webContents.setZoomFactor(target);
        }
    }
    // 页面加载完成后设置（Chromium 会在 did-finish-load 后恢复持久化 zoom）
    mainWindow.webContents.on("did-finish-load", applyZoom);
    // 窗口大小变化时动态调整
    mainWindow.on("resize", applyZoom);
    // 触控板捏合 / Ctrl+Scroll 触发的 Chromium 内置缩放：立刻重置回目标值。
    // 前端 wheel 事件拦截只管 Ctrl+Scroll，捏合走独立通道不走 wheel，这里兜底。
    mainWindow.webContents.on("zoom-changed", (_event, zoomDirection) => {
        void zoomDirection; // 忽略方向，直接重置
        applyZoom();
    });
    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        openWebLink(url);
        return { action: "deny" };
    });
    mainWindow.webContents.on("will-navigate", (event, url) => {
        if (!isTrustedPage(url)) {
            event.preventDefault();
            openWebLink(url);
        }
    });
    mainWindow.webContents.on("will-redirect", (event, url) => {
        if (!isTrustedPage(url))
            event.preventDefault();
    });
    if (isDev) {
        void mainWindow.loadURL(DEV_URL);
    }
    else {
        // file:// 下没有 Vite 代理，前端需要知道后端的绝对地址。
        // 关键：这里必须用 bootInfo.apiBase（服务器模式是线上地址），
        // 不能再硬编码 127.0.0.1 —— 前端 resolveApiBase() 会优先读这个查询参数，
        // 写死本地地址会让服务器模式的客户端仍然去连本机后端。
        // extraResources 把 web/dist 的内容平铺到了 resources/web/，所以这里不能再拼 dist。
        // 打包后 __dirname 在 app.asar/dist 里，用相对路径会指到 asar 内部，必须走 resourcesPath。
        const indexHtml = node_path_1.default.join(process.resourcesPath, "web", "index.html");
        void mainWindow.loadFile(indexHtml, {
            query: { apiBase: bootInfo.apiBase },
        });
    }
}
// 只改属性、不重新绑定，所以用 const（prefer-const）；属性赋值不受影响。
const bootInfo = {
    apiBase: `http://127.0.0.1:${apiPort}`,
    token: "",
    version: electron_1.app.getVersion(),
    platform: process.platform,
};
/**
 * 服务器端模式（发给别人用的版本）。
 *
 * 正式客户端默认连接 HTTPS 服务，由每位用户通过软件账号登录。
 * 不分发共享客户端令牌，用户会话由前端登录流程保存。
 * 开发/验收环境可用 AOHUANG_REMOTE_API_BASE 覆盖服务地址。
 */
const REMOTE_API_BASE = process.env.AOHUANG_REMOTE_API_BASE?.trim() || "https://aohuang888.com/miaoshou-platform";
/** 正式客户端只连接服务器；本地后端仅用于开发。 */
function useRemoteServer() {
    if (electron_1.app.isPackaged)
        return true;
    const flag = (process.env.AOHUANG_LOCAL_BACKEND || "").trim();
    if (flag === "1")
        return false; // 显式要求本地后端（开发/单机测试）
    if (flag === "0")
        return true;
    return !isDev; // 生产默认连服务器，开发默认本地
}
async function loadToken() {
    // 后端首次启动会生成 secret.key；Electron 读出来注入前端
    const fs = await Promise.resolve().then(() => __importStar(require("node:fs/promises")));
    // 必须和后端用的数据目录一致：后端把 secret.key 写在自己数据目录根部。
    // 之前这里固定读 %LOCALAPPDATA%，而数据目录已改到 exe 同级 userdata，
    // 结果拿不到 token，界面只会显示未连接。
    const candidates = [
        dataDir && node_path_1.default.join(dataDir, "secret.key"),
        node_path_1.default.join(process.env.LOCALAPPDATA || electron_1.app.getPath("home"), "aohuang-desktop", "secret.key"),
        node_path_1.default.join(node_path_1.default.dirname(process.execPath), "userdata", "secret.key"),
    ].filter((item) => Boolean(item));
    for (const file of candidates) {
        try {
            const value = (await fs.readFile(file, "utf-8")).trim();
            if (value)
                return value;
        }
        catch {
            continue;
        }
    }
    return "";
}
electron_1.ipcMain.handle("app:boot", () => bootInfo);
electron_1.ipcMain.on("app:boot-sync", (event) => {
    event.returnValue = bootInfo;
});
const sessionVault = (0, sessionVault_1.createSessionVault)(node_path_1.default.join(electron_1.app.getPath("userData"), "operator-session.v1.json"), () => bootInfo.apiBase, electron_1.safeStorage);
electron_1.ipcMain.on("session:read", (event) => {
    event.returnValue = event.sender === mainWindow?.webContents
        && event.senderFrame === mainWindow.webContents.mainFrame
        && isTrustedPage(event.senderFrame.url) ? sessionVault.read() : null;
});
electron_1.ipcMain.on("session:write", (event, value) => {
    if (event.sender !== mainWindow?.webContents
        || event.senderFrame !== mainWindow.webContents.mainFrame
        || !isTrustedPage(event.senderFrame.url)
        || (value !== null && (typeof value !== "string" || value.length > 8192))) {
        event.returnValue = false;
        return;
    }
    event.returnValue = sessionVault.write(value);
});
electron_1.ipcMain.handle("window:minimize", () => mainWindow?.minimize());
electron_1.ipcMain.handle("window:toggle-maximize", () => {
    if (!mainWindow)
        return false;
    if (mainWindow.isMaximized())
        mainWindow.unmaximize();
    else
        mainWindow.maximize();
    return mainWindow.isMaximized();
});
electron_1.ipcMain.handle("window:close", () => mainWindow?.close());
electron_1.ipcMain.handle("window:is-maximized", () => mainWindow?.isMaximized() ?? false);
/* ---------------- 客户端自更新 ----------------
 * 后端在 shared_mode 下不下载（共享服务器装更新没意义，要装的是本机），
 * 所以下载/校验/安装都在主进程做。渲染层只负责显示进度和按钮。
 * 三个入口都要校验来源页面，避免被注入的 iframe 调用来下任意 exe。 */
function trustedSender(event) {
    return event.sender === mainWindow?.webContents
        && event.senderFrame === mainWindow.webContents.mainFrame
        && isTrustedPage(event.senderFrame.url);
}
electron_1.ipcMain.handle("update:pending", async (event) => {
    if (!trustedSender(event))
        return null;
    return (0, updateInstaller_1.pendingInstaller)();
});
electron_1.ipcMain.handle("update:download", async (event, manifest) => {
    if (!trustedSender(event))
        return { ok: false, error: "不可信的调用来源" };
    if (!manifest || typeof manifest !== "object") {
        return { ok: false, error: "缺少更新信息" };
    }
    const result = await (0, updateInstaller_1.downloadInstaller)(manifest, updateInstaller_1.broadcastProgress);
    return result;
});
electron_1.ipcMain.handle("update:install", async (event) => {
    if (!trustedSender(event))
        return { ok: false, error: "不可信的调用来源" };
    return (0, updateInstaller_1.quitAndInstall)();
});
electron_1.ipcMain.handle("update:discard", async (event) => {
    if (!trustedSender(event))
        return { ok: false, error: "不可信的调用来源" };
    await (0, updateInstaller_1.discardInstaller)();
    return { ok: true };
});
electron_1.ipcMain.handle("update:run-installer-now", async (event, installerPath) => {
    if (!trustedSender(event))
        return { ok: false, error: "不可信的调用来源" };
    // 只允许跑我们自己下到 updates 目录里的 exe，不接受任意路径
    const pending = await (0, updateInstaller_1.pendingInstaller)();
    if (!pending || node_path_1.default.resolve(pending.path) !== node_path_1.default.resolve(String(installerPath))) {
        return { ok: false, error: "安装包路径不受信任" };
    }
    return (0, updateInstaller_1.launchInstaller)(pending.path, true);
});
electron_1.app.whenReady().then(async () => {
    // 启动时清掉历史安装包：软件内更新每次都会把 77MB 的 exe 下到
    // userData\updates\，安装器管不到那里。只在后台跑，失败不影响启动。
    void (0, updateInstaller_1.pruneStaleInstallers)();
    if (useRemoteServer()) {
        // 软件账号登录后才取得用户会话；不再读取旧安装目录中的共享令牌。
        bootInfo.apiBase = REMOTE_API_BASE.replace(/\/+$/, "");
        bootInfo.token = "";
        createWindow();
        electron_1.app.on("activate", () => {
            if (electron_1.BrowserWindow.getAllWindows().length === 0)
                createWindow();
        });
        return;
    }
    startServer();
    // 后端可能因端口占用改到 8813+，先探出真正在响应的端口。
    // 健康检查路径必须带 /v1（后端是 /api/v1/system/health），原先漏了 /v1 导致永远超时。
    const health = (port) => `http://127.0.0.1:${port}/api/v1/system/health`;
    let ready = false;
    for (let attempt = 0; attempt < 120 && !ready; attempt += 1) {
        apiPort = await detectApiPort();
        ready = await ping(health(apiPort));
        if (!ready)
            await new Promise((r) => setTimeout(r, 500));
    }
    bootInfo.apiBase = `http://127.0.0.1:${apiPort}`;
    // secret.key 是后端首次启动时生成的，健康检查通过后再读一次即可；仍读不到
    // 说明后端异常，多试几次避免首启动的竞态。
    for (let attempt = 0; attempt < 10 && !bootInfo.token; attempt += 1) {
        bootInfo.token = await loadToken();
        if (!bootInfo.token)
            await new Promise((r) => setTimeout(r, 300));
    }
    if (!ready) {
        console.error(`后端启动超时（试过 ${API_PORT}-${API_PORT_MAX}），窗口仍会打开但接口不可用`);
    }
    if (!bootInfo.token) {
        console.error("读不到访问令牌，界面会提示未连接");
    }
    createWindow();
    electron_1.app.on("activate", () => {
        if (electron_1.BrowserWindow.getAllWindows().length === 0)
            createWindow();
    });
});
electron_1.app.on("window-all-closed", () => {
    if (process.platform !== "darwin")
        electron_1.app.quit();
});
electron_1.app.on("before-quit", () => {
    // 用户点了「重启并安装」才装；单纯关程序不装 —— 否则下次打开发现版本
    // 悄悄变了却不知道为什么。安装器是独立进程，本进程退出不会带走它。
    if ((0, updateInstaller_1.isInstallRequested)()) {
        void (0, updateInstaller_1.installOnQuit)();
    }
    server?.kill();
});
//# sourceMappingURL=main.js.map