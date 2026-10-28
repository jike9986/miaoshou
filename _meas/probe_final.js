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

  // 保证在仓库页
  await ev(`const a=Array.from(document.querySelectorAll('aside a')).find(x=>(x.textContent||'').trim()==='成品仓库'); a.click(); return 1;`);
  await wait(2600);
  // 勾两件
  console.log("勾选:", await ev(`
    const b=document.querySelectorAll('main input[type=checkbox]');
    if(b.length<2) return 'too few';
    b[0].click(); b[1].click(); return b.length;
  `));
  await wait(1200);
  // 开弹窗
  await ev(`const b=Array.from(document.querySelectorAll('main button')).find(x=>(x.textContent||'').includes('价格试算')); if(b)b.click(); return 1;`);
  await wait(2000);
  // 有弹窗才点试算
  console.log("弹窗:", await ev(`return !!document.querySelector('[role=dialog]')`));
  await ev(`const d=document.querySelector('[role=dialog]'); if(d){const b=Array.from(d.querySelectorAll('button')).find(x=>/^试算/.test((x.textContent||'').trim())); if(b)b.click();} return 1;`);
  await wait(7000);
  console.log("最终:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'closed';
    const rows=Array.from(d.querySelectorAll('tbody tr')).map(tr=>Array.from(tr.querySelectorAll('td')).map(td=>td.textContent.trim()));
    return {rowCount: rows.length, rows: rows.slice(0,3), summary: (d.innerText||'').split('\\n').filter(l=>/共 \\d+ 件|模板：|币种：/.test(l))};
  `), null, 2));
  ws.close();
})();
