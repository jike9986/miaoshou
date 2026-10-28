// 定向检查：仓库页图片能否加载、列表懒加载是否符合预期。
import fs from "node:fs";
import path from "node:path";

const PORT = 9333;
const BASE = "http://127.0.0.1:5173";
const OUT = path.resolve(".smoke");
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.handlers = new Map();
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
        return;
      }
      const list = this.handlers.get(msg.method);
      if (list) for (const fn of list) fn(msg.params);
    });
  }
  send(method, params = {}) {
    this.id += 1;
    const id = this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  on(method, fn) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(fn);
  }
}

async function openPage(url) {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(url)}`, {
    method: "PUT",
  });
  const target = await res.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  return { target, cdp: new Cdp(ws) };
}

const { target, cdp } = await openPage(`${BASE}/warehouse`);
await cdp.send("Runtime.enable");
await cdp.send("Page.enable");
await cdp.send("Network.enable");
const failures = [];
cdp.on("Network.loadingFailed", (p) => failures.push({ id: p.requestId, err: p.errorText, type: p.type }));
cdp.on("Network.responseReceived", (p) => {
  if (p.type === "Image" && p.response.status >= 400) {
    failures.push({ url: p.response.url, status: p.response.status });
  }
});
await sleep(3000);

// 进入游客模式，再回仓库
await cdp.send("Runtime.evaluate", {
  expression: `sessionStorage.setItem('aohuang.guest','1'); location.replace('/warehouse');`,
});
await sleep(5000);

const info = await cdp.send("Runtime.evaluate", {
  expression: `JSON.stringify({
    url: location.pathname,
    cards: document.querySelectorAll('article').length,
    imgs: [...document.querySelectorAll('article img')].map(i => ({w: i.naturalWidth, h: i.naturalHeight, src: i.currentSrc.slice(0,60), loading: i.loading})).slice(0,6),
    failedImgs: [...document.querySelectorAll('article img')].filter(i=>i.complete && i.naturalWidth===0).length
  })`,
  returnByValue: true,
});
console.log(info.result.value);
console.log("network failures:", JSON.stringify(failures.slice(0, 10), null, 2));

const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync(path.join(OUT, "warehouse-guest.png"), Buffer.from(shot.data, "base64"));

process.exit(0);
