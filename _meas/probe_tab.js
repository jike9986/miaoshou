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
  await wait(3000);
  console.log("点发布记录标签:", await ev(`
    const m=document.querySelector('main');
    const tabs=Array.from(m.querySelectorAll('button,[role=tab]'));
    const t=tabs.find(x=>/^发布记录$/.test((x.textContent||'').trim()));
    if(!t) return '没找到';
    t.click(); return 'ok';
  `));
  await wait(5000);
  console.log(JSON.stringify(await ev(`
    const m=document.querySelector('main');
    const t=m.innerText||'';
    const inp=Array.from(m.querySelectorAll('input')).map(x=>x.getAttribute('placeholder'));
    return {
      placeholders: inp,
      有搜索: inp.some(p=>p && /搜/.test(p)),
      有状态筛选: /全部状态/.test(t),
      文本片段: t.slice(0,200)
    };
  `), null, 1));
  ws.close(); process.exit(0);
})();
