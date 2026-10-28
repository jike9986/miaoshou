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

  await ev(`location.hash = '#/automation'; return 1;`);
  await wait(2500);
  console.log("勾选前有诊断卡:", await ev(`return (document.body.innerText||'').includes('字段诊断');`));

  // 勾选第一件
  console.log("勾选:", await ev(`
    const cb = document.querySelector('main input[type=checkbox], main button[role=checkbox]');
    if (!cb) return 'none';
    cb.click(); return 'clicked';
  `));
  await wait(6000);
  console.log("勾选后有诊断卡:", await ev(`return (document.body.innerText||'').includes('字段诊断');`));
  console.log("诊断内容:", JSON.stringify(await ev(`
    const t = document.body.innerText || '';
    const i = t.indexOf('字段诊断');
    return i >= 0 ? t.slice(i, i + 220) : null;
  `)));
  ws.close();
})();
