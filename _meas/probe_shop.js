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
  // 目标店铺下拉是自定义 Select，点开看选项
  console.log("店铺下拉:", JSON.stringify(await ev(`
    const d = document.querySelector('[role=dialog]');
    const btns = Array.from(d.querySelectorAll('button'));
    const sel = btns.filter(b => /C lm shop|选择目标店铺/.test(b.textContent||''));
    if (sel.length) { sel[0].click(); return 'opened'; }
    return btns.map(b=>(b.textContent||'').trim()).slice(0,12);
  `)));
  await wait(1200);
  console.log("选项:", JSON.stringify(await ev(`
    const opts = Array.from(document.querySelectorAll('[role=option], li[data-value], [role=listbox] *'));
    return opts.map(o=>(o.textContent||'').trim()).filter(Boolean).slice(0,10);
  `)));
  ws.close();
})();
