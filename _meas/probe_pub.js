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
  await wait(3500);

  // 勾一件，点发布
  console.log("勾选:", await ev(`
    const cb = document.querySelector('main input[type=checkbox], main button[role=checkbox]');
    if (!cb) return 'none';
    cb.click(); return 'ok';
  `));
  await wait(1500);
  console.log("点发布:", await ev(`
    const btn = Array.from(document.querySelectorAll('main button')).find(b=>(b.textContent||'').trim()==='发布');
    if (!btn) return 'no btn';
    btn.click(); return 'ok';
  `));
  await wait(3000);
  console.log("弹窗字段:", JSON.stringify(await ev(`
    const d = document.querySelector('[role=dialog]');
    if (!d) return 'no dialog';
    return {
      hasRate: /汇率/.test(d.innerText||''),
      hasCross: /载体/.test(d.innerText||''),
      text: (d.innerText||'').slice(0, 500)
    };
  `), null, 1));
  ws.close();
})();
