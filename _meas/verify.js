const http = require("node:http");
const fs = require("node:fs");

function getJson(p) {
  return new Promise((res, rej) => {
    http.get({ host: "127.0.0.1", port: 9333, path: p }, (r) => {
      let b = "";
      r.on("data", (c) => (b += c));
      r.on("end", () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } });
    }).on("error", rej);
  });
}

const EXPR = [
  "(function(){",
  "var txt = document.body.innerText;",
  "var grid = document.querySelector('ul.grid');",
  "var cards = grid ? Array.prototype.slice.call(grid.querySelectorAll(':scope > li')) : [];",
  "var labels = Array.prototype.slice.call(document.querySelectorAll('label')).filter(function(l){",
  "  var t = l.innerText.trim();",
  "  return t === '文字' || t === '图片' || t === '定价模板' || t === '发布到店铺';",
  "});",
  "var ys = labels.map(function(l){ return Math.round(l.getBoundingClientRect().y); });",
  "var first = cards[0];",
  "var thumb = first ? first.querySelector('img') : null;",
  "var btns = Array.prototype.slice.call(document.querySelectorAll('button'));",
  "var out = {};",
  "out.connPanel = txt.indexOf('APP ID') >= 0;",
  "out.breadcrumb = txt.indexOf('功能 / 自动化上品') >= 0;",
  "out.headerDesc = /采集箱共/.test(txt);",
  "out.totalText = /共 \\d+ 条/.test(txt);",
  "out.pageNum = /第 \\d+ \\/ \\d+ 页/.test(txt);",
  "out.selectAllFiltered = txt.indexOf('全选筛选结果') >= 0;",
  "out.allBtn = btns.filter(function(b){ return b.innerText.trim() === '全选'; }).length;",
  "out.sizeChartHint = txt.indexOf('点击缩略图选择') >= 0;",
  "out.startBtns = btns.filter(function(b){ return b.innerText.indexOf('开始处理') >= 0; }).length;",
  "out.warehouseBtn = btns.some(function(b){ return b.innerText.trim() === '成品仓库'; });",
  "out.cardText = first ? first.innerText.split('\\n').join(' | ') : null;",
  "out.gridCols = grid ? getComputedStyle(grid).gridTemplateColumns : null;",
  "out.cardCount = cards.length;",
  "out.switchCount = labels.length;",
  "out.switchYs = ys;",
  "out.switchSameRow = ys.length === 4 && ys[0] === ys[1] && ys[1] === ys[2] && ys[2] === ys[3];",
  "out.thumb = thumb ? { w: Math.round(thumb.getBoundingClientRect().width), h: Math.round(thumb.getBoundingClientRect().height) } : null;",
  "out.scrollW = document.documentElement.scrollWidth;",
  "out.clientW = document.documentElement.clientWidth;",
  "return out;",
  "})()",
].join("\n");

(async () => {
  const list = await getJson("/json/list");
  const page = list.find((t) => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const send = (m, p) =>
    new Promise((res, rej) => {
      const i = ++id;
      pending.set(i, { res, rej });
      ws.send(JSON.stringify({ id: i, method: m, params: p }));
    });
  ws.addEventListener("message", (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
    }
  });
  await new Promise((r) => ws.addEventListener("open", r));

  const r = await send("Runtime.evaluate", { expression: EXPR, returnByValue: true });
  const out =
    r.result && r.result.value
      ? r.result.value
      : { cdpError: r.exceptionDetails || r.result };
  fs.writeFileSync("C:\\AohuangDesktop\\_verify.json", JSON.stringify(out, null, 2), "utf-8");
  ws.close();
  process.exit(0);
})().catch((e) => {
  fs.writeFileSync("C:\\AohuangDesktop\\_verify.json", JSON.stringify({ error: String(e) }, null, 2), "utf-8");
  process.exit(1);
});
