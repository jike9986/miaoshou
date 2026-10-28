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
  // 关抽屉
  await ev(`const d=document.querySelector('[role=dialog]'); if(d){const b=Array.from(d.querySelectorAll('button')).find(x=>(x.textContent||'').trim()==='关闭'); if(b)b.click();} return 1;`);
  await wait(1200);
  // 排序改「最近入库」并翻到能找到 id=3：直接搜 detailId
  await ev(`
    const inp = document.querySelector('main input[type=search], main input[placeholder*="搜索"]');
    if (!inp) return 'no search';
    const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
    s.call(inp, '3484413012');
    inp.dispatchEvent(new Event('input', {bubbles:true}));
    return 'typed';
  `);
  await wait(3000);
  console.log("搜索结果:", JSON.stringify(await ev(`
    const arts = Array.from(document.querySelectorAll('main article'));
    return arts.map(a => (a.querySelector('h3')||{}).textContent).slice(0,5);
  `)));
  console.log("点开:", await ev(`
    const a = document.querySelector('main article button');
    if(!a) return 'none'; a.click(); return 'clicked';
  `));
  await wait(3500);
  console.log("翻译检查:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'none';
    const t=d.innerText||''; const i=t.indexOf('翻译检查');
    return i>=0 ? t.slice(i, i+500) : 'no section';
  `)));
  ws.close();
})();
