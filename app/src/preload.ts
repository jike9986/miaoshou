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
  // 前端启动参数由主进程给出（含访问令牌），避免在页面里硬编码
  boot: () => ipcRenderer.invoke("app:boot"),
});
