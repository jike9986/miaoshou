const PORT = 9333;
(async () => {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find(t => t.type === "page");
  const ws = new globalThis.WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  await new Promise(r => { ws.onopen = r; });
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); p(m.result); } };
  const send = (method, params) => new Promise(res => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({id:i, method, params: params||{}})); });
  await send("Runtime.enable");
  const ev = async (body) => {
    const r = await send("Runtime.evaluate", { expression: `(() => {\n${body}\n})()`, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return {ERR: r.exceptionDetails.exception?.description};
    return r.result.value;
  };
  const wait = ms => new Promise(r=>setTimeout(r,ms));

  // 装 hook
  await ev(`
    if (!window.__hook3) { window.__hook3 = 1; window.__u = [];
      const o = window.fetch; window.fetch = function(...a){
        const u = typeof a[0]==='string'?a[0]:(a[0]&&a[0].url);
        if (u && u.includes('/warehouse/')) window.__u.push(u);
        return o.apply(this,a);
      };
    }
    window.__u = []; return 1;
  `);
  // 直接点第一张卡片（成品仓库列表）
  console.log("点卡片:", await ev(`
    const main = document.querySelector('main');
    const btn = Array.from(main.querySelectorAll('[class*=cursor-pointer], button, article, a'))
      .find(el => el.querySelector('img'));
    if (!btn) return 'none';
    btn.click(); return btn.tagName;
  `));
  await wait(3500);
  console.log("详情 URL:", JSON.stringify(await ev("return window.__u")));
  console.log("抽屉:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    return d ? {len:(d.innerText||'').length, head:(d.innerText||'').slice(0,200)} : 'none';
  `)));
  ws.close();
})();
