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
  await ev(`const c=document.querySelector('main article'); if(c){const b=c.querySelector('button'); if(b)b.click();} return 1;`);
  await wait(3500);
  await ev(`const d=document.querySelector('[role=dialog]'); const b=d&&Array.from(d.querySelectorAll('button')).find(x=>/识别主图文字/.test(x.textContent||'')); if(b)b.click(); return 1;`);
  await wait(16000);
  // 切一张为翻译
  await ev(`
    const d=document.querySelector('[role=dialog]');
    const sw=Array.from(d.querySelectorAll('button')).filter(x=>/改为翻译/.test(x.textContent||''));
    if(sw.length) sw[0].click();
    return sw.length;
  `);
  await wait(800);
  console.log("提交:", await ev(`
    const d=document.querySelector('[role=dialog]');
    const b=Array.from(d.querySelectorAll('button')).find(x=>/提交翻译/.test(x.textContent||''));
    if(!b) return 'none'; b.click(); return 'ok';
  `));
  await wait(20000);
  console.log("提交后任务区:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'dialog closed';
    const t=d.innerText||'';
    const i=t.indexOf('图片翻译任务');
    return i<0 ? '无该区' : t.slice(i, i+220);
  `)));
  ws.close(); process.exit(0);
})();
