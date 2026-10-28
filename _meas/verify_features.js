// 验证新增/修复的四个能力在真实运行环境里存在且可用。
//
// 关键教训（两次误判）：
//   · Electron 是 file:// 协议，main.tsx 用 HashRouter，路由在 location.hash
//   · 导航必须点侧边栏 <a>，不要碰 history.pushState（pathname 会变成 /C:/...）
//   · Runtime.evaluate 的表达式必须包在 IIFE 里，顶层 return 是语法错误
//
// 输出：C:\AohuangDesktop\_verify.json

const fs = require("fs");
const path = require("path");

const OUT = "C:\\AohuangDesktop\\_verify.json";
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
      const resolve = pending.get(m.id);
      pending.delete(m.id);
      resolve(m.result);
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

  // 点侧边栏链接导航，等 SPA 渲染，再断言 hash
  // 点击后轮询等 hash 生效，不靠固定 sleep（首屏还在渲染时点不动）
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
          await wait(900); // 等页面内容渲染完
          return hash;
        }
      }
    }
    const hash = await ev("return location.hash;");
    throw new Error(`${label} 导航失败: hash=${hash} 期望=${href}`);
  };

  try {
    // ---- 1. 导航项 ----
    results.navItems = await ev(`
      return Array.from(document.querySelectorAll('aside a'))
        .map(a => (a.textContent || '').trim()).filter(Boolean);
    `);
    results.hasImagesNav = results.navItems.includes("图片翻译");

    // ---- 2. 图片翻译页 ----
    results.imagesHash = await goto("图片翻译");
    results.imagesTitle = await ev(`
      const h = document.querySelector('main h1, main h2, [class*=PageHeader] h1');
      return h ? h.textContent.trim() : null;
    `);
    results.imagesText = await ev(
      "return (document.querySelector('main')?.innerText || '').slice(0, 600);"
    );
    results.imagesHasAnalyzeBtn = await ev(`
      return Array.from(document.querySelectorAll('main button'))
        .some(b => /分析|识别/.test(b.textContent || ''));
    `);
    results.imagesHasTranslateBtn = await ev(`
      return Array.from(document.querySelectorAll('main button'))
        .some(b => /翻译/.test(b.textContent || ''));
    `);

    // ---- 3. 版本号 ----
    results.versionText = await ev(`
      const el = Array.from(document.querySelectorAll('aside *'))
        .find(n => n.children.length === 0 &&
          /v\\d+\\.\\d+\\.\\d+|发现新版本|已是最新|检查更新/.test(n.textContent || ''));
      return el ? el.textContent.trim() : null;
    `);

    // ---- 4. 成品仓库 + 价格试算 ----
    results.warehouseHash = await goto("成品仓库");
    results.warehouseTitle = await ev(`
      const h = document.querySelector('main h1, main h2');
      return h ? h.textContent.trim() : null;
    `);

    // 勾第一件商品
    results.checked = await ev(`
      const boxes = document.querySelectorAll('main input[type=checkbox]');
      if (!boxes.length) return false;
      boxes[0].click();
      return true;
    `);
    await wait(1200);

    results.priceBtn = await ev(`
      const b = Array.from(document.querySelectorAll('main button'))
        .find(x => (x.textContent || '').includes('价格试算'));
      return b ? { text: b.textContent.trim(), disabled: b.disabled } : null;
    `);

    // 真的点开弹窗，看对话框是否渲染
    if (results.priceBtn && !results.priceBtn.disabled) {
      await ev(`
        const b = Array.from(document.querySelectorAll('main button'))
          .find(x => (x.textContent || '').includes('价格试算'));
        b.click();
        return true;
      `);
      await wait(1500);
      results.priceDialog = await ev(`
        const d = document.querySelector('[role=dialog]');
        if (!d) return null;
        return {
          title: (d.querySelector('h2, [class*=title]')?.textContent || '').trim(),
          hasCalcButton: Array.from(d.querySelectorAll('button'))
            .some(b => /试算/.test(b.textContent || '')),
          text: (d.innerText || '').slice(0, 200),
        };
      `);
      // 关掉
      await ev(`
        const d = document.querySelector('[role=dialog]');
        const b = d && Array.from(d.querySelectorAll('button')).find(x => (x.textContent||'').trim() === '关闭');
        if (b) b.click();
        return true;
      `);
    }

    results.ok = true;
  } catch (error) {
    results.ok = false;
    results.error = String(error && error.message ? error.message : error);
  } finally {
    close();
    fs.mkdirSync(path.dirname(OUT), { recursive: true });
    fs.writeFileSync(OUT, JSON.stringify(results, null, 2), "utf8");
    console.log(JSON.stringify(results, null, 2));
  }
})();
