// 验证采集箱已接上、认领跳转不再落到死路由。
//
// 背景：CollectBoxPage.tsx（1362 行）之前是孤立的，/collect 与 /collect-box
// 都重定向到 /automation。仓库页「认领到采集箱」调 navigate("/collect")，
// 结果被重定向走，点了没反应。
//
// 输出：C:\AohuangDesktop\_verify_collect.json

const fs = require("fs");

const OUT = "C:\\AohuangDesktop\\_verify_collect.json";
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

  const goto = async (label, tries = 3) => {
    let href = null;
    for (let attempt = 1; attempt <= tries; attempt++) {
      href = await ev(`
        const a = Array.from(document.querySelectorAll('aside a'))
          .find(x => (x.textContent || '').trim() === ${JSON.stringify(label)});
        if (!a) return null;
        a.click();
        return a.getAttribute('href');
      `);
      if (!href) throw new Error("侧边栏没有入口: " + label);
      for (let i = 0; i < 12; i++) {
        await wait(250);
        const hash = await ev("return location.hash;");
        if (hash.startsWith(href)) {
          await wait(1000);
          return hash;
        }
      }
    }
    const hash = await ev("return location.hash;");
    throw new Error(`${label} 导航失败: hash=${hash} 期望=${href}`);
  };

  try {
    // ---- 1. 导航项顺序 ----
    results.navItems = await ev(`
      return Array.from(document.querySelectorAll('aside a'))
        .map(a => (a.textContent || '').trim()).filter(Boolean);
    `);
    results.hasCollectNav = results.navItems.includes("采集箱");
    results.collectIsFirst = results.navItems[0] === "采集箱";

    // ---- 2. 采集箱页真的能打开 ----
    results.collectHash = await goto("采集箱");
    results.collectTitle = await ev(`
      const h = document.querySelector('main h1, main h2');
      return h ? h.textContent.trim() : null;
    `);
    results.collectText = await ev(
      "return (document.querySelector('main')?.innerText || '').slice(0, 500);"
    );
    // 页面上应该有筛选和分页，说明是完整页面而不是空壳
    results.collectControls = await ev(`
      const main = document.querySelector('main');
      if (!main) return null;
      return {
        selects: main.querySelectorAll('select').length,
        buttons: main.querySelectorAll('button').length,
        hasSearch: !!main.querySelector('input'),
      };
    `);

    // ---- 3. 旧地址 /collect-box 应重定向到 /collect ----
    await ev("location.hash = '#/collect-box'; return true;");
    await wait(2200);
    results.collectBoxRedirectsTo = await ev("return location.hash;");

    // ---- 4. 仓库页「认领到采集箱」跳转正确 ----
    results.warehouseHash = await goto("成品仓库");
    // 勾一件，让批量操作条出现
    results.checked = await ev(`
      const boxes = document.querySelectorAll('main input[type=checkbox]');
      if (!boxes.length) return false;
      boxes[0].click();
      return true;
    `);
    await wait(1200);

    results.claimButtonExists = await ev(`
      return Array.from(document.querySelectorAll('button'))
        .some(b => (b.textContent || '').includes('认领到采集箱'));
    `);

    // 点它，看落到哪个路由
    if (results.claimButtonExists) {
      await ev(`
        const b = Array.from(document.querySelectorAll('button'))
          .find(x => (x.textContent || '').includes('认领到采集箱'));
        b.click();
        return true;
      `);
      // 有账号会直接跳 /collect；没连接会跳 /unlock，两种都算合理
      await wait(2500);
      results.afterClaimHash = await ev("return location.hash;");
      results.claimLandsOnCollect =
        results.afterClaimHash === "#/collect" ||
        results.afterClaimHash.startsWith("#/unlock");
      results.claimLandsOnDeadRoute =
        results.afterClaimHash.startsWith("#/automation");
    }

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
