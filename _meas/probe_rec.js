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
  await wait(4500);
  await ev(`const c=document.querySelector('main article'); if(c){const b=c.querySelector('button'); if(b)b.click();} return 1;`);
  await wait(4500);
  console.log(JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'no dialog';
    const t=d.innerText||'';
    return {
      有对账按钮: /查询已提交任务/.test(t),
      有额度: /今日 \\d+\\//.test(t),
      有识别: /识别主图文字/.test(t),
      有提交翻译: /提交翻译/.test(t)
    };
  `), null, 1));
  // 点对账
  console.log("点对账:", await ev(`
    const d=document.querySelector('[role=dialog]');
    const b=Array.from(d.querySelectorAll('button')).find(x=>/查询已提交任务/.test(x.textContent||''));
    if(!b) return 'none'; b.click(); return 'ok';
  `));
  await wait(6000);
  console.log("toast:", JSON.stringify(await ev(`
    return Array.from(document.querySelectorAll('[data-sonner-toast] li, li'))
      .map(x=>(x.innerText||'').trim()).filter(t=>/任务|核对/.test(t)).slice(0,2);
  `)));
  ws.close(); process.exit(0);
})();
