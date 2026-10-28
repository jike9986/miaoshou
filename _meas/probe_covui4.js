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
  console.log("展开:", await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'no dialog';
    const s=Array.from(d.querySelectorAll('summary')).find(x=>/逐字段明细/.test(x.textContent||''));
    if(!s) return 'no summary'; s.click(); return 'ok';
  `));
  await wait(1500);
  console.log(JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    const lis=Array.from(d.querySelectorAll('li')).filter(x=>/=(已翻译|保留|缺失|无需翻译|未提供)/.test((x.innerText||'').replace(/\\s+/g,' ')));
    const rows=lis.map(x=>(x.innerText||'').replace(/\\s+/g,' ').trim());
    const kinds={};
    rows.forEach(r=>{ const m=r.match(/(已翻译|保留|缺失|无需翻译|未提供)$/); if(m) kinds[m[1]]=(kinds[m[1]]||0)+1; });
    return {行数:rows.length, 状态统计:kinds, 前5行:rows.slice(0,5)};
  `), null, 1));
  ws.close(); process.exit(0);
})();
