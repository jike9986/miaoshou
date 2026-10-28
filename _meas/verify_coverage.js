// 验证「翻译检查」区块在商品详情抽屉里真的渲染出漏翻字段。
//
// 前置：id=3 这件商品入仓时翻译了标题，但 SKU 属性名（颜色/尺码）仍是中文，
// 后端 translation_coverage 判定 incomplete，列出 5 条 missing 路径。
//
// 输出：C:\AohuangDesktop\_verify_coverage.json

const fs = require("fs");

const OUT = "C:\\AohuangDesktop\\_verify_coverage.json";
const PORT = 9333;

async function cdp() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find((t) => t.type === "page");
  if (!page) throw new Error("没有页面 target");
  const ws = new globalThis.WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = reject;
  });
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) {
      const res = pending.get(m.id);
      pending.delete(m.id);
      res(m.result);
    }
  };
  const send = (method, params) =>
    new Promise((res) => {
      const i = ++id;
      pending.set(i, res);
      ws.send(JSON.stringify({ id: i, method, params: params || {} }));
    });
  await send("Runtime.enable");
  const ev = async (body) => {
    const r = await send("Runtime.evaluate", {
      expression: "(() => {\n" + body + "\n})()",
      returnByValue: true,
      awaitPromise: true,
    });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description || "evaluate 失败");
    }
    return r.result.value;
  };
  return { ev, close: () => ws.close() };
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const { ev, close } = await cdp();
  const results = {};

  try {
    // 到成品仓库
    await ev(`
      const a = Array.from(document.querySelectorAll('aside a'))
        .find(x => (x.textContent || '').trim() === '成品仓库');
      a.click(); return true;
    `);
    for (let i = 0; i < 14; i++) {
      await wait(250);
      const h = await ev("return location.hash;");
      if (h.startsWith("#/warehouse")) break;
    }
    await wait(1800);
    results.hash = await ev("return location.hash;");

    // 第一张卡片的标题是「A」（id=3 那件商品），点它开详情
    results.opened = await ev(`
      const cards = Array.from(document.querySelectorAll('main button, main [role=button]'));
      const first = cards.find(c => (c.textContent || '').includes('A') &&
        c.querySelector('img'));
      if (first) { first.click(); return 'card'; }
      // 兜底：点第一张图片
      const img = document.querySelector('main img');
      if (img) { (img.closest('button') || img).click(); return 'img'; }
      return null;
    `);
    await wait(2800);

    // 抽屉里找「翻译检查」
    results.drawerOpen = await ev(`
      return !!document.querySelector('[role=dialog], aside, [data-state=open]');
    `);
    results.hasCoverageSection = await ev(`
      const body = document.body.innerText || '';
      return body.includes('翻译检查');
    `);
    results.coverageText = await ev(`
      const t = document.body.innerText || '';
      const idx = t.indexOf('翻译检查');
      return idx >= 0 ? t.slice(idx, idx + 420) : null;
    `);

    // 检查是否列出了具体的 missing 路径
    results.listsSkuPaths = await ev(`
      const t = document.body.innerText || '';
      return t.includes('skuPropertyList');
    `);
    results.showsPendingBadge = await ev(`
      const t = document.body.innerText || '';
      return /\\d+ 处待补/.test(t);
    `);

    results.ok = true;
  } catch (error) {
    results.ok = false;
    results.error = String(error && error.message ? error.message : error);
  } finally {
    close();
    fs.writeFileSync(OUT, JSON.stringify(results, null, 2), "utf8");
    console.log(JSON.stringify(results, null, 2));
  }
})();
