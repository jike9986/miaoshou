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
  console.log(JSON.stringify(await ev(`
    const m=document.querySelector('main');
    const sws=Array.from(m.querySelectorAll('[role=switch]')).map(x=>({
      label: x.getAttribute('aria-label'), checked: x.getAttribute('aria-checked'), tag: x.tagName
    }));
    const cbs=Array.from(m.querySelectorAll('input[type=checkbox]')).map(x=>({
      label: (x.closest('label')||{}).innerText, checked: x.checked
    }));
    return {switches: sws, checkboxes: cbs};
  `), null, 1));
  ws.close(); process.exit(0);
})();
