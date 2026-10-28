// 通过 CDP HTTP + Runtime.evaluate 读取真实 DOM 测量（用 Electron 自带 node 跑）
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const LOG = "C:\\AohuangDesktop\\_measure.json";

function getJson(p) {
  return new Promise((resolve, reject) => {
    http
      .get({ host: "127.0.0.1", port: 9333, path: p }, (res) => {
        let b = "";
        res.on("data", (c) => (b += c));
        res.on("end", () => {
          try {
            resolve(JSON.parse(b));
          } catch (e) {
            reject(e);
          }
        });
      })
      .on("error", reject);
  });
}

const EXPR = `(() => {
  const q = (s) => document.querySelector(s);
  const R = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) };
  };
  const txt = document.body.innerText;
  const grid = q('ul.grid');
  const cards = grid ? [...grid.querySelectorAll(':scope > li')] : [];
  const header = q('header');
  const headerText = header ? header.innerText.replace(/\\n+/g, ' | ') : '';
  return {
    viewport: { w: window.innerWidth, h: window.innerHeight },
    dpr: window.devicePixelRatio,
    headerText,
    headerHasBreadcrumb: /功能\\s*\\/\\s*自动化上品/.test(headerText),
    bodyHasBreadcrumb: /功能\\s*\\/\\s*自动化上品/.test(txt),
    bodyHasConnectionPanel: txt.includes('连接妙手') && (txt.includes('APP ID') || txt.includes('已连接')),
    grid: R(grid),
    gridCols: grid ? getComputedStyle(grid).gridTemplateColumns : null,
    cardCount: cards.length,
    firstCard: R(cards[0]),
    fifthCard: R(cards[4]),
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
    numbers: [...document.querySelectorAll('input[type=number]')].map((n) => ({
      v: n.value,
      label: n.closest('label')?.innerText?.trim().split('\\n')[0]?.slice(0, 10) || '',
      y: Math.round(n.getBoundingClientRect().y),
      x: Math.round(n.getBoundingClientRect().x),
    })),
    startButtons: [...document.querySelectorAll('button')].filter((b) => b.innerText.includes('开始处理')).map((b) => R(b)),
    warehouseButtonInHeader: [...document.querySelectorAll('button')].some((b) => b.innerText.trim() === '成品仓库'),
    sizeChartCount: (txt.match(/尺码图/g) || []).length,
    progressPanel: txt.includes('处理状态') && txt.includes('任务摘要'),
    accountSwitchPresent: !!q('[aria-label="切换账号"]') || txt.includes('罗蔓'),
    shopSwitchPresent: !!q('[aria-label="当前店铺"]'),
  };
})()`;

(async () => {
  const list = await getJson("/json/list");
  const page = list.find((t) => t.type === "page");
  if (!page) throw new Error("no page");

  // 用 Electron 内置 node 环境跑 WebSocket 太重，这里直接走 CDP 的 /json 无法 evaluate。
  // 改为用 Node 原生 WebSocket（Node 22+ 内置）
  const WS = globalThis.WebSocket;
  if (!WS) throw new Error("no global WebSocket in this node");
  const ws = new WS(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const send = (method, params) =>
    new Promise((resolve, reject) => {
      const mid = ++id;
      pending.set(mid, { resolve, reject });
      ws.send(JSON.stringify({ id: mid, method, params }));
    });
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      if (m.error) reject(new Error(JSON.stringify(m.error)));
      else resolve(m.result);
    }
  });
  await new Promise((r) => ws.addEventListener("open", r));
  const res = await send("Runtime.evaluate", { expression: EXPR, returnByValue: true });
  fs.writeFileSync(LOG, JSON.stringify(res.result.value, null, 2), "utf-8");
  ws.close();
  process.exit(0);
})().catch((e) => {
  fs.writeFileSync(LOG, JSON.stringify({ error: String(e) }, null, 2), "utf-8");
  process.exit(1);
});
