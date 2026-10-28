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
  // 定时任务页：新建任务应带出账号 5 的模板
  await ev(`location.hash='#/schedules'; return 1;`);
  await wait(5000);
  await ev(`
    const m=document.querySelector('main');
    const b=Array.from(m.querySelectorAll('button')).find(x=>/新建任务/.test(x.textContent||''));
    if(b) b.click(); return 1;
  `);
  await wait(4000);
  console.log("定时新建表单模板:" , JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'no dialog';
    const selects=Array.from(d.querySelectorAll('select'));
    const out=selects.map(s=>({v:s.value, t:(s.options[s.selectedIndex]||{}).text}));
    const t=d.innerText||'';
    const i=t.indexOf('定价模板');
    return {selects:out, 定价模板附近: i<0?'无':t.slice(i,i+60)};
  `)));
  ws.close(); process.exit(0);
})();
