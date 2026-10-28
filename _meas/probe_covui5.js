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
  console.log(JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    const det=d&&d.querySelector('details');
    if(!det) return 'no details';
    const open=det.hasAttribute('open');
    const ul=det.querySelector('ul');
    const lis=ul?Array.from(ul.querySelectorAll('li')):[];
    const kinds={};
    lis.forEach(li=>{ const t=(li.innerText||'').replace(/\\s+/g,' ').trim();
      const m=t.match(/(已翻译|保留|缺失|无需翻译|未提供)$/); if(m) kinds[m[1]]=(kinds[m[1]]||0)+1; });
    return {open, li数:lis.length, 状态统计:kinds,
            前3:(lis.slice(0,3).map(x=>(x.innerText||'').replace(/\\s+/g,' ').trim()))};
  `), null, 1));
  ws.close(); process.exit(0);
})();
