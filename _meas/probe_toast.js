const PORT = 9333;
(async () => {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find(t => t.type === "page");
  const ws = new globalThis.WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map(); const toasts = [];
  await new Promise(r => { ws.onopen = r; });
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); p(m.result); return; }
    if (m.method === "Runtime.consoleAPICalled") toasts.push((m.params.args||[]).map(a=>String(a.value??a.description??'')).join(" "));
  };
  const send = (method, params) => new Promise(res => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({id:i, method, params: params||{}})); });
  await send("Runtime.enable");
  const ev = async (body) => {
    const r = await send("Runtime.evaluate", { expression: `(() => {\n${body}\n})()`, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return {ERR: r.exceptionDetails.exception?.description};
    return r.result.value;
  };
  const wait = ms => new Promise(r=>setTimeout(r,ms));

  await ev(`const d=document.querySelector('[role=dialog]'); if(d){const b=Array.from(d.querySelectorAll('button')).find(x=>(x.textContent||'').trim()==='关闭'); if(b)b.click();} return true;`);
  await wait(1000);
  await ev(`const b=Array.from(document.querySelectorAll('main button')).find(x=>(x.textContent||'').includes('价格试算')); if(b)b.click(); return true;`);
  await wait(1800);
  // 全页文本，看有没有 toast 或错误
  console.log("弹窗内全部文本:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    return d ? (d.innerText||'') : 'no dialog';
  `)));
  console.log("页面 body 尾部（找 toast）:", JSON.stringify(await ev("return (document.body.innerText||'').slice(-400)")));
  ws.close();
})();
