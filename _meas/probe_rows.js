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
  // 刷新页面拿新数据
  await ev(`location.reload(); return 1;`);
  await wait(6000);
  await ev(`location.hash='#/schedules'; return 1;`);
  await wait(3500);
  await ev(`
    const m=document.querySelector('main');
    const tabs=Array.from(m.querySelectorAll('button,[role=tab]'));
    const t=tabs.find(x=>/^发布记录$/.test((x.textContent||'').trim()));
    if(t) t.click(); return 1;
  `);
  await wait(5000);
  console.log("行数:", await ev(`
    return document.querySelectorAll('main tbody tr').length;
  `));
  console.log("搜索前文本:", await ev(`
    const m=document.querySelector('main');
    const t=m.innerText||'';
    const i=t.indexOf('本机');
    return i<0 ? t.slice(-200) : t.slice(i-40, i+120);
  `));
  // 用真实鼠标事件输入搜索词
  console.log("输入 Yoga:", await ev(`
    const inp=Array.from(document.querySelectorAll('main input')).find(x=>/搜商品/.test(x.getAttribute('placeholder')||''));
    if(!inp) return 'no input';
    const r=inp.getBoundingClientRect();
    return {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)};
  `));
  ws.close(); process.exit(0);
})();
