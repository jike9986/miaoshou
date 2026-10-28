// 验证定价模板编辑器：打开 → 字段齐全 → 展开折叠区 → 改名保存 → 库里真的有。
//
// 输出：C:\AohuangDesktop\_verify_template.json

const fs = require("fs");

const OUT = "C:\\AohuangDesktop\\_verify_template.json";
const PORT = 9333;
const API = "http://127.0.0.1:8812/api/v1";
const KEY = "C:\\AohuangDesktop\\userdata\\secret.key";

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

  const goto = async (label) => {
    const href = await ev(`
      const a = Array.from(document.querySelectorAll('aside a'))
        .find(x => (x.textContent || '').trim() === ${JSON.stringify(label)});
      if (!a) return null;
      a.click();
      return a.getAttribute('href');
    `);
    if (!href) throw new Error("侧边栏没有入口: " + label);
    for (let i = 0; i < 14; i++) {
      await wait(250);
      const hash = await ev("return location.hash;");
      if (hash.startsWith(href)) {
        await wait(1200);
        return hash;
      }
    }
    throw new Error("导航失败: " + label);
  };

  /** 总是操作最上层的那个 dialog（堆叠时最后一个 DOM 节点在最上面）。 */
  const topDialog = `(() => {
    const ds = document.querySelectorAll('[role=dialog]');
    return ds.length ? ds[ds.length - 1] : null;
  })()`;

  try {
    results.hash = await goto("自动化上品");

    // 打开选用弹窗
    await ev(`
      const row = Array.from(document.querySelectorAll('main span'))
        .find(s => (s.textContent || '').includes('定价模板') && s.querySelector('button'));
      const b = row && row.querySelector('button');
      if (b) b.click();
      return true;
    `);
    await wait(1500);
    results.dialogsAfterPick = await ev(
      "return document.querySelectorAll('[role=dialog]').length;"
    );

    // 点「编辑模板」
    await ev(`
      const ds = document.querySelectorAll('[role=dialog]');
      const d = ds[ds.length - 1];
      const b = Array.from(d.querySelectorAll('button'))
        .find(x => (x.textContent || '').includes('编辑模板'));
      if (b) b.click();
      return true;
    `);
    await wait(2200);

    // 关键断言：只剩一个 dialog
    results.dialogsAfterEdit = await ev(
      "return document.querySelectorAll('[role=dialog]').length;"
    );

    results.editor = await ev(`
      const ds = document.querySelectorAll('[role=dialog]');
      const d = ds[ds.length - 1];
      if (!d) return null;
      const t = d.innerText || '';
      return {
        inputs: d.querySelectorAll('input').length,
        hasName: t.includes('模板名称'),
        hasProfit: t.includes('利润率'),
        hasLastMile: t.includes('尾程运费'),
        hasAdvanced: t.includes('一般无需修改'),
        hasSave: Array.from(d.querySelectorAll('button'))
          .some(b => (b.textContent || '').includes('保存模板')),
      };
    `);

    // 展开折叠区
    await ev(`
      const ds = document.querySelectorAll('[role=dialog]');
      const d = ds[ds.length - 1];
      const b = Array.from(d.querySelectorAll('button'))
        .find(x => (x.textContent || '').includes('一般无需修改'));
      if (b) b.click();
      return true;
    `);
    await wait(900);
    results.advanced = await ev(`
      const ds = document.querySelectorAll('[role=dialog]');
      const d = ds[ds.length - 1];
      const t = d.innerText || '';
      return {
        hasVat: t.includes('VAT'),
        hasBcp: t.includes('BCP'),
        hasFirstWeight: t.includes('首重'),
        inputs: d.querySelectorAll('input').length,
      };
    `);

    // 真保存：写一个探针模板
    results.save = await ev(`
      const key = ${JSON.stringify(KEY)};
      return 'skip-client-read';
    `);
    // 通过页面内 fetch 走应用自己的鉴权链路：直接把模板名改成探针再点保存
    results.renamed = await ev(`
      const ds = document.querySelectorAll('[role=dialog]');
      const d = ds[ds.length - 1];
      // 第一个 text input 是模板名称
      const nameInput = Array.from(d.querySelectorAll('input'))
        .find(i => i.value === '默认模板' || /模板|name/i.test(i.getAttribute('placeholder') || ''));
      if (!nameInput) return null;
      const before = nameInput.value;
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(nameInput, '__ui_probe__');
      nameInput.dispatchEvent(new Event('input', { bubbles: true }));
      return { before, after: nameInput.value };
    `);
    await wait(600);

    // 点保存
    results.clickedSave = await ev(`
      const ds = document.querySelectorAll('[role=dialog]');
      const d = ds[ds.length - 1];
      const b = Array.from(d.querySelectorAll('button'))
        .find(x => (x.textContent || '').trim() === '保存模板');
      if (!b) return false;
      b.click();
      return true;
    `);
    await wait(3000);

    // 查库里有没有 __ui_probe__
    results.afterSaveHasProbe = await ev(`
      return fetch('${API}/system/templates', {
        headers: { 'Authorization': 'Bearer ' + (window.__tk || '') }
      }).then(r => r.json()).then(j => {
        const names = (j.templates || []).map(t => t.name);
        return { names, hasProbe: names.includes('__ui_probe__') };
      }).catch(e => ({ error: String(e) }));
    `);
    results.dialogTextTail = await ev(`
      const ds = document.querySelectorAll('[role=dialog]');
      const d = ds[ds.length - 1];
      return d ? (d.innerText || '').slice(-200) : null;
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
