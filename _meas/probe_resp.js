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
  // 直接看响应体
  const res = await ev(`
    return fetch('http://127.0.0.1:8812/api/v1/warehouse/price-preview', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + (document.cookie.match(/token=([^;]+)/)?.[1] || window.__tok || '')
      },
      body: JSON.stringify({items:[{warehouseId:'355'}], template:'默认模板'})
    }).then(async r => ({status: r.status, body: (await r.text()).slice(0,300)})).catch(e => ({err: String(e)}));
  `);
  console.log("直连测试:", JSON.stringify(res, null, 2));
  ws.close();
})();
