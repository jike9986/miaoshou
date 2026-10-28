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
  const snap = async (tag) => {
    const s = await ev(`
      const d=document.querySelector('[role=dialog]');
      if(!d) return 'closed';
      const btn=Array.from(d.querySelectorAll('button')).find(x=>/^试算/.test((x.textContent||'').trim()));
      return {tag:${JSON.stringify(tag)}, tr:d.querySelectorAll('tbody tr').length, btnText:btn?btn.textContent.trim():null, btnDisabled:btn?btn.disabled:null, len:(d.innerText||'').length};
    `);
    console.log(tag, JSON.stringify(s));
  };
  await snap("点击前");
  console.log("click:", await ev(`
    const d=document.querySelector('[role=dialog]');
    const btn=Array.from(d.querySelectorAll('button')).find(x=>/^试算/.test((x.textContent||'').trim()));
    if(!btn) return 'no btn';
    btn.click(); return 'ok';
  `));
  for (const ms of [300, 800, 1500, 3000, 6000]) { await wait(ms); await snap("+"+ms); }
  ws.close();
})();
