// 验证：导航恢复成老版四项，且「认领到采集箱」原地调接口不跳页。
//
// 老版侧边栏只有 4 项（自动化 / 违规词库 / 成品仓库 / 定时发布）+ 连接妙手。
// 采集箱是自动化页的数据源，图片翻译是处理设置里的开关，都不是页面。
//
// 输出：C:\AohuangDesktop\_verify_nav.json

const fs = require("fs");

const OUT = "C:\\AohuangDesktop\\_verify_nav.json";
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
    // ---- 1. 导航必须正好是老版四项 + 连接妙手 ----
    results.navItems = await ev(`
      return Array.from(document.querySelectorAll('aside a'))
        .map(a => (a.textContent || '').trim()).filter(Boolean);
    `);
    const expected = ["自动化上品", "成品仓库", "定时发布", "违规词库", "连接妙手"];
    results.navMatchesOldApp =
      JSON.stringify(results.navItems) === JSON.stringify(expected);
    results.hasCollectNav = results.navItems.includes("采集箱");
    results.hasImagesNav = results.navItems.includes("图片翻译");

    // ---- 2. 旧地址应回落到自动化页 ----
    const redirects = {};
    for (const [hash, want] of [
      ["#/collect", "#/automation"],
      ["#/collect-box", "#/automation"],
      ["#/images", "#/automation"],
    ]) {
      await ev(`location.hash = ${JSON.stringify(hash)}; return true;`);
      await wait(1600);
      redirects[hash] = await ev("return location.hash;");
      redirects[hash + "_ok"] = redirects[hash] === want;
    }
    results.redirects = redirects;

    // ---- 3. 仓库页认领按钮：原地调接口，不跳页 ----
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
    await wait(1500);
    results.warehouseHash = await ev("return location.hash;");

    // 勾一件
    await ev(`
      const b = document.querySelectorAll('main input[type=checkbox]');
      if (b.length) b[0].click();
      return true;
    `);
    await wait(1200);

    results.claimButtonExists = await ev(`
      return Array.from(document.querySelectorAll('button'))
        .some(b => (b.textContent || '').includes('认领到采集箱'));
    `);

    // 拦截 fetch 看有没有真的发 /publish
    await ev(`
      if (!window.__claimHook) {
        window.__claimHook = true;
        window.__claimCalls = [];
        const orig = window.fetch;
        window.fetch = function(...args) {
          const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url);
          if (url && url.includes('/publish')) {
            window.__claimCalls.push({ url, body: args[1] && args[1].body });
          }
          return orig.apply(this, args);
        };
      }
      window.__claimCalls = [];
      return true;
    `);

    // 点认领
    await ev(`
      const b = Array.from(document.querySelectorAll('button'))
        .find(x => (x.textContent || '').includes('认领到采集箱'));
      if (b) b.click();
      return true;
    `);
    await wait(2500);

    results.hashAfterClaim = await ev("return location.hash;");
    results.didNotNavigate = results.hashAfterClaim.startsWith("#/warehouse");
    results.publishCalls = await ev("return (window.__claimCalls || []).slice(0, 3);");
    results.sentClaimOnly = (results.publishCalls || []).some((c) => {
      try {
        return JSON.parse(c.body || "{}").claimOnly === true;
      } catch (e) {
        return false;
      }
    });

    // 页面上的提示（toast）
    results.toastText = await ev(`
      const t = document.body.innerText || '';
      const m = t.match(/已认领[^\\n]{0,40}|认领需要[^\\n]{0,30}|请先选择[^\\n]{0,20}|认领失败[^\\n]{0,40}/);
      return m ? m[0] : null;
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
