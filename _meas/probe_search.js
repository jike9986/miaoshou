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
  await ev(`location.hash='#/publish'; return 1;`);
  await wait(5000);
  console.log("发布记录页:", JSON.stringify(await ev(`
    const m=document.querySelector('main');
    if(!m) return 'no main';
    const t=m.innerText||'';
    const inp=m.querySelector('input[placeholder*="搜"]');
    return {
      有搜索框: !!inp,
      占位符: inp ? inp.getAttribute('placeholder') : null,
      有状态筛选: /全部状态|待发布|已发布/.test(t),
      行数: m.querySelectorAll('tbody tr').length
    };
  `)));
  ws.close(); process.exit(0);
})();
