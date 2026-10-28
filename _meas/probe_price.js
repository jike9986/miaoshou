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
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description);
    return r.result.value;
  };
  const wait = ms => new Promise(r => setTimeout(r, ms));

  // 确保在仓库页并勾选一件
  console.log("导航:", await ev(`
    const a = Array.from(document.querySelectorAll('aside a')).find(x => (x.textContent||'').trim()==='成品仓库');
    a.click(); return a.getAttribute('href');
  `));
  await wait(2500);
  console.log("hash:", await ev("return location.hash"));
  await ev(`const b=document.querySelectorAll('main input[type=checkbox]'); if(b.length) b[0].click(); return true;`);
  await wait(1000);
  // 开弹窗
  await ev(`const b=Array.from(document.querySelectorAll('main button')).find(x=>(x.textContent||'').includes('价格试算')); b.click(); return true;`);
  await wait(1800);
  // 看模板是否自动选中
  console.log("模板选中值:", await ev(`
    const d=document.querySelector('[role=dialog]');
    const s=d && d.querySelector('select');
    return s ? {value:s.value, options:Array.from(s.options).map(o=>o.value).slice(0,4)} : 'no select';
  `));
  // 点试算
  console.log("点试算:", await ev(`
    const d=document.querySelector('[role=dialog]');
    const b=Array.from(d.querySelectorAll('button')).find(x=>/^试算/.test((x.textContent||'').trim()));
    if(!b) return 'no button'; b.click(); return b.textContent.trim();
  `));
  await wait(6000);
  console.log("结果:", await ev(`
    const d=document.querySelector('[role=dialog]');
    return {text:(d.innerText||'').slice(0,700), hasTable: !!d.querySelector('table'), rows: d.querySelectorAll('tbody tr').length};
  `));
  ws.close();
})();
