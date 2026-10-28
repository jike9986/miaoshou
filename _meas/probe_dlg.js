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
  await new Promise(r=>setTimeout(r,1000));
  console.log("dialog 数量:", await ev("return document.querySelectorAll('[role=dialog]').length"));
  console.log("每个 dialog:", JSON.stringify(await ev(`
    return Array.from(document.querySelectorAll('[role=dialog]')).map((d,i)=>({
      i, head: (d.innerText||'').slice(0,60).replace(/\\n/g,' | '),
      inputs: d.querySelectorAll('input').length
    }));
  `), null, 2));
  ws.close();
})();
