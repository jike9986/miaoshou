const PORT = 9333;
(async () => {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find(t => t.type === "page");
  const ws = new globalThis.WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  await new Promise(r => { ws.onopen = r; });
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); p(m.result); } };
  const send = (method, params) => new Promise(res => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({id:i, method, params: params||{}})); });
  await send("Runtime.enable"); await send("Network.enable");
  const ev = async (body) => {
    const r = await send("Runtime.evaluate", { expression: `(() => {\n${body}\n})()`, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return {ERR: r.exceptionDetails.exception?.description};
    return r.result.value;
  };
  console.log("详情请求 URL:", JSON.stringify(await ev(`
    if (!window.__dh) { window.__dh = 1; window.__urls = [];
      const o = window.fetch; window.fetch = function(...a){
        const u = typeof a[0]==='string'?a[0]:(a[0]&&a[0].url);
        if (u && u.includes('/warehouse/')) window.__urls.push(u);
        return o.apply(this,a);
      };
    }
    return window.__urls.slice(-6);
  `)));
  console.log("抽屉文本前 500:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    return d ? (d.innerText||'').slice(0,500) : 'no dialog';
  `)));
  ws.close();
})();
