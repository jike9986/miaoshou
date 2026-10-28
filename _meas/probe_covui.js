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
  await ev(`location.reload(); return 1;`);
  await wait(6500);
  await ev(`location.hash='#/warehouse'; return 1;`);
  await wait(4500);
  // 找 detailId 3484413012 的卡片
  console.log("找商品:", await ev(`
    const arts=Array.from(document.querySelectorAll('main article'));
    const t=arts.find(a=>/3484413012/.test(a.innerText||'')) || arts[0];
    if(!t) return 'no card';
    const b=t.querySelector('button'); if(b) b.click();
    return (t.innerText||'').slice(0,60);
  `));
  await wait(4500);
  console.log(JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'no dialog';
    const t=d.innerText||'';
    const i=t.indexOf('翻译检查');
    return {
      翻译检查片段: i<0?'无':t.slice(i, i+200),
      有明细开关: /逐字段明细/.test(t)
    };
  `), null, 1));
  // 展开明细
  console.log("展开:", await ev(`
    const d=document.querySelector('[role=dialog]');
    const s=Array.from(d.querySelectorAll('summary')).find(x=>/逐字段明细/.test(x.textContent||''));
    if(!s) return 'no summary'; s.click(); return 'ok';
  `));
  await wait(1500);
  console.log(JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    const t=d.innerText||'';
    const i=t.indexOf('逐字段明细');
    return {展开后: i<0?'无':t.slice(i, i+260)};
  `), null, 1));
  ws.close(); process.exit(0);
})();
