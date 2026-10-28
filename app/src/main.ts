import { app, BrowserWindow, ipcMain, shell } from "electron";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { spawn, ChildProcess } from "node:child_process";

// 本机实测结论（决定性实验）：
//   在 m.js 第一行写日志，然后无参数启动 electron —— 日志文件根本不生成。
//   说明崩溃发生在**加载 JS 之前**的原生初始化阶段，不是我们的代码。
//   只加 --user-data-dir        → 仍然崩（所以 userData 不是原因）
//   只加 --no-sandbox           → 正常到 ready
// 也就是说 appendSwitch 在这里只是给「开发态 pnpm dev」兜底，
// 真正让 exe 起来的是命令行上的 --no-sandbox（由 launch.bat / 快捷方式提供）。
app.commandLine.appendSwitch("no-sandbox");
app.commandLine.appendSwitch("disable-gpu-sandbox");

// userData 仍需指到可写位置：本机 %APPDATA% 下写不进去时，
// 即便沙箱问题已解决，Chromium 初始化 profile 也会失败（另一条崩溃路径）。
// 逐个候选尝试，第一个可写的就用。
{
  const candidates = [
    process.env.AOHUANG_DATA_DIR && path.join(process.env.AOHUANG_DATA_DIR, "electron"),
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "aohuang-desktop", "electron"),
    path.join(os.tmpdir(), "aohuang-desktop-electron"),
    path.join(path.dirname(app.getPath("exe")), "userdata"),
  ].filter((item): item is string => Boolean(item));

  for (const dir of candidates) {
    try {
      fs.mkdirSync(dir, { recursive: true });
      fs.accessSync(dir, fs.constants.W_OK);
      app.setPath("userData", dir);
      break;
    } catch {
      /* 换下一个候选 */
    }
  }
}
import http from "node:http";

const DEV_URL = "http://127.0.0.1:5173";
// 后端会从 API_PORT 起向上找一个没被占用的端口（见 server/main.py 的 pick_port）。
// 所以这里不能写死 8812：必须启动后探测实际端口，再把它交给前端。
const API_PORT = 8812;
// 与后端 core/config.py 的 api_port_max 保持一致（默认 8822），否则后端换到
// 8816+ 时 Electron 探不到，窗口会报接口不可用。
const API_PORT_MAX = 8822;
const isDev = !app.isPackaged;

let mainWindow: BrowserWindow | null = null;
/** 实际交给后端的数据目录；loadToken 必须从这里读 secret.key，两处不能各算各的 */
let dataDir = "";
let server: ChildProcess | null = null;
/** 后端实际监听的端口；启动探测成功后写入，供 bootInfo / 窗口 query 使用 */
let apiPort = API_PORT;

function ping(url: string, timeout = 800): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
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
async function detectApiPort(): Promise<number> {
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
function pickDataDir(): string {
  const candidates = [
    path.join(path.dirname(process.execPath), "userdata"),
    app.getPath("userData"),
    path.join(os.tmpdir(), "aohuang-desktop"),
  ];
  for (const dir of candidates) {
    try {
      fs.mkdirSync(dir, { recursive: true });
      // 真写一次才算数：mkdir 成功不代表能建文件
      const probe = path.join(dir, ".write-probe");
      fs.writeFileSync(probe, "ok");
      fs.unlinkSync(probe);
      return dir;
    } catch {
      /* 换下一个 */
    }
  }
  return candidates[candidates.length - 1];
}

function startServer(): void {
  if (isDev) {
    server = spawn("python", ["-m", "uvicorn", "main:app", "--port", String(API_PORT)], {
      cwd: path.join(__dirname, "..", "..", "server"),
      stdio: "inherit",
      windowsHide: true,
    });
    return;
  }
  // 打包后运行 PyInstaller 生成的后端可执行文件（不需要用户装 Python）
  const bundled = path.join(process.resourcesPath, "server", "aohuang-server.exe");

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
  const logDir = path.join(dataDir, "logs");
  let out: number | "ignore" = "ignore";
  try {
    fs.mkdirSync(logDir, { recursive: true });
    out = fs.openSync(path.join(logDir, "server.out.log"), "a");
  } catch {
    out = "ignore";
  }

  server = spawn(bundled, [], {
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

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1180,
    minHeight: 720,
    show: false,
    frame: false,
    titleBarStyle: "hidden",
    backgroundColor: "#F6F8F9",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  mainWindow.once("ready-to-show", () => mainWindow?.show());

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  if (isDev) {
    void mainWindow.loadURL(DEV_URL);
  } else {
    // file:// 下没有 Vite 代理，前端需要知道后端的绝对地址（用探测到的真实端口）。
    // extraResources 把 web/dist 的内容平铺到了 resources/web/，所以这里不能再拼 dist。
    // 打包后 __dirname 在 app.asar/dist 里，用相对路径会指到 asar 内部，必须走 resourcesPath。
    const indexHtml = path.join(process.resourcesPath, "web", "index.html");
    void mainWindow.loadFile(indexHtml, {
      query: { apiBase: `http://127.0.0.1:${apiPort}` },
    });
  }
}

// 只改属性、不重新绑定，所以用 const（prefer-const）；属性赋值不受影响。
const bootInfo: { apiBase: string; token: string; version: string; platform: string } = {
  apiBase: `http://127.0.0.1:${apiPort}`,
  token: "",
  version: app.getVersion(),
  platform: process.platform,
};

async function loadToken(): Promise<string> {
  // 后端首次启动会生成 secret.key；Electron 读出来注入前端
  const fs = await import("node:fs/promises");
  // 必须和后端用的数据目录一致：后端把 secret.key 写在自己数据目录根部。
  // 之前这里固定读 %LOCALAPPDATA%，而数据目录已改到 exe 同级 userdata，
  // 结果拿不到 token，界面只会显示未连接。
  const candidates = [
    dataDir && path.join(dataDir, "secret.key"),
    path.join(process.env.LOCALAPPDATA || app.getPath("home"), "aohuang-desktop", "secret.key"),
    path.join(path.dirname(process.execPath), "userdata", "secret.key"),
  ].filter((item): item is string => Boolean(item));
  for (const file of candidates) {
    try {
      const value = (await fs.readFile(file, "utf-8")).trim();
      if (value) return value;
    } catch {
      continue;
    }
  }
  return "";
}

ipcMain.handle("app:boot", () => bootInfo);
ipcMain.on("app:boot-sync", (event) => {
  event.returnValue = bootInfo;
});
ipcMain.handle("window:minimize", () => mainWindow?.minimize());
ipcMain.handle("window:toggle-maximize", () => {
  if (!mainWindow) return false;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
  return mainWindow.isMaximized();
});
ipcMain.handle("window:close", () => mainWindow?.close());
ipcMain.handle("window:is-maximized", () => mainWindow?.isMaximized() ?? false);

app.whenReady().then(async () => {
  startServer();
  // 后端可能因端口占用改到 8813+，先探出真正在响应的端口。
  // 健康检查路径必须带 /v1（后端是 /api/v1/system/health），原先漏了 /v1 导致永远超时。
  const health = (port: number) => `http://127.0.0.1:${port}/api/v1/system/health`;
  let ready = false;
  for (let attempt = 0; attempt < 120 && !ready; attempt += 1) {
    apiPort = await detectApiPort();
    ready = await ping(health(apiPort));
    if (!ready) await new Promise((r) => setTimeout(r, 500));
  }
  bootInfo.apiBase = `http://127.0.0.1:${apiPort}`;
  // secret.key 是后端首次启动时生成的，健康检查通过后再读一次即可；仍读不到
  // 说明后端异常，多试几次避免首启动的竞态。
  for (let attempt = 0; attempt < 10 && !bootInfo.token; attempt += 1) {
    bootInfo.token = await loadToken();
    if (!bootInfo.token) await new Promise((r) => setTimeout(r, 300));
  }
  if (!ready) {
    console.error(`后端启动超时（试过 ${API_PORT}-${API_PORT_MAX}），窗口仍会打开但接口不可用`);
  }
  if (!bootInfo.token) {
    console.error("读不到访问令牌，界面会提示未连接");
  }
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  server?.kill();
});
