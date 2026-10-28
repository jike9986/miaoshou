// 全量回归：导航 / 翻译检查 / 保存前校验 / 失败明细。
//
// 输出：C:\AohuangDesktop\_verify_all.json

const fs = require("fs");

const OUT = "C:\\AohuangDesktop\\_verify_all.json";
const PORT = 9333;
const API = "http://127.0.0.1:8812/api/v1";

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
  const ev = async (body, isAsync = false) => {
    const r = await send("Runtime.evaluate", {
      expression: (isAsync ? "(async () => {\n" : "(() => {\n") + body + "\n})()",
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
    // ---- 1. 导航必须正好是老版四项 + 连接妙手 ----
    results.navItems = await ev(`
      return Array.from(document.querySelectorAll('aside a'))
        .map(a => (a.textContent || '').trim()).filter(Boolean);
    `);
    const expected = ["自动化上品", "成品仓库", "定时发布", "违规词库", "连接妙手"];
    results.navOk = JSON.stringify(results.navItems) === JSON.stringify(expected);

    // ---- 2. 旧地址回落 ----
    const redirects = {};
    for (const hash of ["#/collect", "#/collect-box", "#/images"]) {
      await ev(`location.hash = ${JSON.stringify(hash)}; return true;`);
      await wait(1500);
      redirects[hash] = await ev("return location.hash;");
    }
    results.redirects = redirects;
    results.redirectsOk = Object.values(redirects).every((h) => h === "#/automation");

    // ---- 3. 自动化页：进度面板应含任务摘要与处理状态 ----
    await ev(`
      const a = Array.from(document.querySelectorAll('aside a'))
        .find(x => (x.textContent || '').trim() === '自动化上品');
      a.click(); return true;
    `);
    for (let i = 0; i < 14; i++) {
      await wait(250);
      const h = await ev("return location.hash;");
      if (h.startsWith("#/automation")) break;
    }
    await wait(1800);
    results.automationText = await ev(
      "return (document.querySelector('main')?.innerText || '').slice(0, 300);"
    );
    results.hasSummary = await ev(
      "return (document.body.innerText || '').includes('任务摘要');"
    );
    results.hasStatus = await ev(
      "return (document.body.innerText || '').includes('处理状态');"
    );
    results.hasPriceTemplate = await ev(`
      const t = document.body.innerText || '';
      return t.includes('定价模板');
    `);

    // ---- 4. 后端：保存前校验必须拦住漏翻商品 ----
    results.guard = await ev(`
      const key = await fetch('${API}/auth/status').then(r => r.json());
      return { connected: key.connected, accountId: key.accountId };
    `, true);
    // 用页面内 fetch 走应用鉴权，测 process（save=true）
    results.saveGuard = await ev(`
      return fetch('${API}/collect-box/3484413012/process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ accountId: '5', shopId: '18077147', language: 'ms', save: true })
      }).then(async r => {
        const j = await r.json();
        return {
          status: r.status,
          code: j.error && j.error.code,
          message: j.error && j.error.message,
          issues: (j.error && j.error.details && j.error.details.issues) || [],
        };
      }).catch(e => ({ error: String(e) }));
    `, true);
    results.guardBlocks = results.saveGuard.status === 422 &&
      (results.saveGuard.issues || []).some((s) => s.includes("translation coverage"));

    // ---- 5. 详情页翻译检查区块 ----
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
    // 搜到有 coverage 记录的那件
    await ev(`
      const inp = document.querySelector('main input[type=search], main input[placeholder*="搜索"]');
      if (!inp) return false;
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(inp, '3484413012');
      inp.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    `);
    await wait(3000);
    await ev(`
      const b = document.querySelector('main article button');
      if (b) b.click();
      return true;
    `);
    await wait(3000);
    results.detailCoverage = await ev(`
      const d = document.querySelector('[role=dialog]');
      if (!d) return null;
      const t = d.innerText || '';
      const idx = t.indexOf('翻译检查');
      return {
        hasSection: idx >= 0,
        snippet: idx >= 0 ? t.slice(idx, idx + 260) : null,
      };
    `);
    results.coverageRendered = Boolean(
      results.detailCoverage && results.detailCoverage.hasSection
    );

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
