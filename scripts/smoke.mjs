// 运行时验收：用 CDP 驱动无头 Chrome
// 1) 打开各业务页，采集控制台错误与页面异常；
// 2) 先在 /unlock 点「稍后连接」进入游客模式，再验证本地可用的仓库/词库/发布页真能渲染数据；
// 3) 逐页截图到 tk-desktop/.smoke。
// 仅本地开发用，不参与构建。
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
// 截图目录放到 web 之外，避免 Vite 文件监听把 Chrome 的 profile 当成工程文件（EBUSY）
const OUT = path.resolve(".smoke");
const PORT = 9333;
const BASE = "http://127.0.0.1:5173";

fs.mkdirSync(OUT, { recursive: true });

let chrome = null;
async function ensureChrome() {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/json/version`);
    if (res.ok) return;
  } catch {
    /* 需要自己启动 */
  }
  chrome = spawn(
    CHROME,
    [
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${path.join(OUT, "profile")}`,
      "--headless=new",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-gpu",
      "--window-size=1440,940",
      "about:blank",
    ],
    { stdio: "ignore", detached: false }
  );
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await ensureChrome();

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
  close() {
    this.ws.close();
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

const IGNORE = [
  /React Router Future Flag/,
  /Download the React DevTools/,
  /favicon\.ico/,
];

function keep(text) {
  return !IGNORE.some((re) => re.test(text));
}

async function evaluate(cdp, expression) {
  const res = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (res.exceptionDetails) throw new Error(res.exceptionDetails.text);
  return res.result.value;
}

const report = [];

const PAGES = [
  ["warehouse", "/warehouse", 5200],
  ["collect", "/collect", 3500],
  ["schedules", "/schedules", 3000],
  ["words", "/words", 3500],
  ["publish", "/publish", 3000],
  ["images", "/images", 5000],
  ["unlock", "/unlock", 2500],
];

for (const [name, route, wait] of PAGES) {
  const { target, cdp } = await openPage(`${BASE}${route}`);
  const logs = [];
  cdp.on("Runtime.consoleAPICalled", (params) => {
    if (params.type === "error" || params.type === "warning") {
      const text = params.args
        .map((a) => a.value ?? a.description ?? a.type)
        .join(" ")
        .slice(0, 400);
      if (keep(text)) logs.push({ kind: `console.${params.type}`, text });
    }
  });
  cdp.on("Runtime.exceptionThrown", (params) => {
    const text = `${params.exceptionDetails.text} ${
      params.exceptionDetails.exception?.description ?? ""
    }`.slice(0, 500);
    if (keep(text)) logs.push({ kind: "exception", text });
  });
  cdp.on("Log.entryAdded", (params) => {
    if (params.entry.level === "error") {
      const text = String(params.entry.text).slice(0, 400);
      if (keep(text)) logs.push({ kind: "log.error", text });
    }
  });

  await cdp.send("Runtime.enable");
  await cdp.send("Log.enable");
  await cdp.send("Page.enable");
  await sleep(wait);

  const dom = JSON.parse(
    await evaluate(
      cdp,
      "JSON.stringify({title:document.title,url:location.pathname,cards:document.querySelectorAll('article').length,rows:document.querySelectorAll('tbody tr').length,text:(document.body.innerText||'').slice(0,1400)})"
    )
  );

  const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, `${name}.png`), Buffer.from(shot.data, "base64"));

  report.push({ name, route, logs, dom });
  cdp.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`);
}

/* ------------------------------------------------------------------ *
 * 交互验证：游客模式下的仓库（KPI/卡片/分页）、搜索防抖、批量选择条
 * ------------------------------------------------------------------ */

const flow = [];
{
  const { target, cdp } = await openPage(`${BASE}/unlock`);
  const logs = [];
  cdp.on("Runtime.exceptionThrown", (params) =>
    logs.push({ kind: "exception", text: params.exceptionDetails.text })
  );
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
  await sleep(2600);

  // 点「稍后连接」
  const clicked = await evaluate(
    cdp,
    `(() => {
       const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '稍后连接');
       if (!btn) return 'no-button';
       btn.click();
       return 'clicked';
     })()`
  );
  await sleep(5200);

  const state = JSON.parse(
    await evaluate(
      cdp,
      `JSON.stringify({
         url: location.pathname,
         kpi: [...document.querySelectorAll('p')].map(p=>p.textContent).filter(t=>/^[0-9,]+$/.test(t||'')).slice(0,4),
         cards: document.querySelectorAll('article').length,
         imgs: [...document.querySelectorAll('article img')].slice(0,1).map(i=>i.getAttribute('src')),
         pagination: (document.body.innerText.match(/共 [\\d,]+ 条/)||[''])[0],
         cats: [...document.querySelectorAll('button[aria-pressed]')].map(b=>b.textContent.trim()).slice(0,8)
       })`
    )
  );

  // 搜索防抖：输入关键词后 400ms 内不应立即发请求，1s 后列表应变化
  const searchResult = await evaluate(
    cdp,
    `(async () => {
       const input = document.querySelector('input[type=search]');
       if (!input) return { error: 'no-search' };
       const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
       setter.call(input, 'yoga');
       input.dispatchEvent(new Event('input', { bubbles: true }));
       await new Promise(r => setTimeout(r, 1600));
       const cards = document.querySelectorAll('article').length;
       const total = (document.body.innerText.match(/共 [\\d,]+ 条/)||[''])[0];
       return { cards, total };
     })()`
  );

  // 选中第一张卡片 → 浮动批量操作条出现
  const bulk = await evaluate(
    cdp,
    `(async () => {
       const box = document.querySelector('article input[type=checkbox]');
       if (!box) return { error: 'no-checkbox' };
       box.click();
       await new Promise(r => setTimeout(r, 700));
       const bar = document.querySelector('[role=toolbar][aria-label="批量操作"]');
       return { visible: Boolean(bar), text: bar ? bar.innerText.replace(/\\n/g,' | ') : '' };
     })()`
  );

  const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, "flow-warehouse.png"), Buffer.from(shot.data, "base64"));

  flow.push({ clicked, state, searchResult, bulk, logs });
  cdp.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`);
}

/* ------------------------------------------------------------------ *
 * 输出
 * ------------------------------------------------------------------ */

fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify({ report, flow }, null, 2));

for (const item of report) {
  console.log(`\n===== ${item.name} ${item.route} → ${item.dom.url} =====`);
  console.log(`cards=${item.dom.cards} rows=${item.dom.rows}`);
  console.log("text:", item.dom.text.replace(/\n+/g, " | ").slice(0, 700));
  if (item.logs.length === 0) console.log("logs: (none)");
  for (const log of item.logs) console.log(`  [${log.kind}] ${log.text}`);
}

console.log("\n===== flow（/unlock → 稍后连接 → 仓库）=====");
console.log(JSON.stringify(flow, null, 2));

if (chrome) chrome.kill();
process.exit(0);
