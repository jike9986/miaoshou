const fs = require("node:fs");
const { app, BrowserWindow } = require("electron");
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("no-sandbox");
app.whenReady().then(async () => {
  const w = new BrowserWindow({ width: 1600, height: 950, show: false });
  await w.loadURL("http://127.0.0.1:4317/index.html?apiBase=http://127.0.0.1:8812");
  await new Promise(r => setTimeout(r, 6000));
  await w.loadURL("http://localhost:4317/").catch(()=>{});
  app.quit();
});
setTimeout(() => process.exit(0), 20000);
