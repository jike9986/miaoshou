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
  console.log("已选数:", await ev(`
    const t = document.body.innerText||'';
    const m = t.match(/已选 \d+/); return m ? m[0] : 'no';
  `));
  console.log("右栏全部标题:", JSON.stringify(await ev(`
    const cards = Array.from(document.querySelectorAll('h3, [class*=CardTitle]'));
    return cards.map(c => (c.textContent||'').trim()).filter(Boolean).slice(0, 20);
  `)));
  console.log("页面含字段诊断:", await ev(`return (document.body.innerText||'').includes('字段诊断');`));
  console.log("页面含任务摘要:", await ev(`return (document.body.innerText||'').includes('任务摘要');`));
  ws.close();
})();
