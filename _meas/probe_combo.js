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
  await ev(`location.hash='#/automation'; return 1;`);
  await wait(6000);
  const read = async () => ev(`
    const m=document.querySelector('main');
    const t=m.innerText||'';
    const warnings=['都关着','定价模板需要','发布已开启'].filter(w=>t.includes(w));
    const start=Array.from(m.querySelectorAll('button')).find(x=>/开始处理/.test(x.textContent||''));
    return {警告:warnings, 开始禁用: start?start.disabled:null, 开始title:start?start.title:null};
  `);
  console.log("当前:", JSON.stringify(await read()));
  // 关掉文字
  console.log("关文字:", await ev(`
    const m=document.querySelector('main');
    const labs=Array.from(m.querySelectorAll('label'));
    const l=labs.find(x=>/文字/.test(x.textContent||''));
    if(!l) return 'no label';
    const inp=l.querySelector('input');
    if(inp && inp.checked) inp.click();
    return inp ? inp.checked : 'no input';
  `));
  await wait(2500);
  console.log("关文字后:", JSON.stringify(await read()));
  ws.close(); process.exit(0);
})();
