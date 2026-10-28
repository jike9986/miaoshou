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
  await ev(`location.hash='#/schedules'; return 1;`);
  await wait(4000);
  console.log("页面标题:", await ev(`return (document.querySelector('main')?.innerText||'').slice(0,200);`));
  console.log("新建按钮:", await ev(`
    const b=Array.from(document.querySelectorAll('button')).find(x=>/新建/.test(x.textContent||''));
    if(!b) return 'none'; b.click(); return 'ok';
  `));
  await wait(2500);
  console.log("抽屉字段:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'no dialog';
    return (d.innerText||'').slice(0,400);
  `), null, 1));
  ws.close();
})();
