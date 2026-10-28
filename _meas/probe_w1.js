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
  await ev(`location.hash='#/warehouse'; return 1;`);
  await wait(4000);
  console.log("勾选:", await ev(`
    const cb=document.querySelector('main input[type=checkbox]');
    if(!cb) return 'no cb';
    cb.click(); return 'ok';
  `));
  await wait(2000);
  console.log("有选中:", await ev(`
    const t=document.body.innerText||''; return /已选 \\d+/.test(t) ? t.match(/已选 \\d+/)[0] : 'none';
  `));
  console.log("发布按钮:", await ev(`
    const btns=Array.from(document.querySelectorAll('main button')).map(b=>(b.textContent||'').trim()).filter(Boolean);
    return btns.slice(0,14);
  `));
  ws.close();
})();
