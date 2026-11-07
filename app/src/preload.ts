import { contextBridge, ipcRenderer } from "electron";

// 渲染层在页面脚本执行前就需要 apiBase/token，因此用同步 IPC 取值
const boot = ipcRenderer.sendSync("app:boot-sync");

contextBridge.exposeInMainWorld("__DSH_BOOT__", boot);

contextBridge.exposeInMainWorld("desktop", {
  minimize: () => ipcRenderer.invoke("window:minimize"),
  toggleMaximize: () => ipcRenderer.invoke("window:toggle-maximize"),
  close: () => ipcRenderer.invoke("window:close"),
  isMaximized: () => ipcRenderer.invoke("window:is-maximized"),
  platform: process.platform,
  sessionToken: {
    read: (): string | null => ipcRenderer.sendSync("session:read"),
    write: (token: string | null): boolean => ipcRenderer.sendSync("session:write", token),
  },
  // 前端启动参数由主进程给出（含访问令牌），避免在页面里硬编码
  boot: () => ipcRenderer.invoke("app:boot"),
  /**
   * 客户端自更新。后端在 shared_mode 下不下载更新包（共享服务器装更新
   * 没有意义，要装的是本机），所以下载/校验/安装全部由主进程完成。
   * 这里只暴露「发起下载 / 查询已下载 / 重启安装 / 放弃」四个动作，
   * 不暴露任意路径执行。
   */
  update: {
    pending: () => ipcRenderer.invoke("update:pending"),
    download: (manifest: {
      version: string;
      url: string;
      sha256: string;
      sizeBytes?: number;
      notes?: string;
    }) => ipcRenderer.invoke("update:download", manifest),
    install: () => ipcRenderer.invoke("update:install"),
    discard: () => ipcRenderer.invoke("update:discard"),
    runInstallerNow: (installerPath: string) =>
      ipcRenderer.invoke("update:run-installer-now", installerPath),
    /** 订阅下载进度；返回取消订阅函数 */
    onProgress: (handler: (progress: {
      received: number;
      total: number;
      percent: number;
    }) => void) => {
      const listener = (_event: unknown, progress: {
        received: number;
        total: number;
        percent: number;
      }): void => handler(progress);
      ipcRenderer.on("update:progress", listener);
      return () => ipcRenderer.removeListener("update:progress", listener);
    },
  },
});
