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
  await ev(`
    if (!window.__hook4) { window.__hook4 = 1; window.__u = [];
      const o = window.fetch; window.fetch = function(...a){
        const u = typeof a[0]==='string'?a[0]:(a[0]&&a[0].url);
        if (u && u.includes('/warehouse/')) window.__u.push(u);
        return o.apply(this,a);
      };
    }
    window.__u = []; return 1;
  `);
  // 找标题为 A 的那张卡片里的 button
  console.log("点开:", await ev(`
    const articles = Array.from(document.querySelectorAll('main article'));
    const target = articles.find(a => {
      const h = a.querySelector('h3');
      return h && (h.textContent||'').trim() === 'A';
    });
    if (!target) return 'no card A; 前3个标题: ' + articles.slice(0,3).map(a=>(a.querySelector('h3')||{}).textContent).join(' | ');
    const b = target.querySelector('button');
    if (!b) return 'no button';
    b.click(); return 'clicked';
  `));
  await wait(3500);
  console.log("详情 URL:", JSON.stringify(await ev("return window.__u")));
  console.log("抽屉文本:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'none';
    const t = d.innerText||'';
    const i = t.indexOf('翻译检查');
    return {len:t.length, coverage: i>=0 ? t.slice(i, i+400) : null};
  `)));
  ws.close();
})();
