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
  console.log("复选框数量:", await ev(`
    const main = document.querySelector('main');
    return main.querySelectorAll('[role=checkbox], input[type=checkbox], button[aria-checked]').length;
  `));
  console.log("点第一张卡片:", await ev(`
    const main = document.querySelector('main');
    const label = main.querySelector('label[class*=grid]');
    if (!label) return 'no label';
    label.click(); return 'clicked';
  `));
  await wait(7000);
  console.log("已选:", await ev(`
    const m = (document.body.innerText||'').match(/已选 \\d+/); return m ? m[0] : 'no';
  `));
  console.log("有字段诊断:", await ev(`return (document.body.innerText||'').includes('字段诊断');`));
  console.log("右栏标题:", JSON.stringify(await ev(`
    return Array.from(document.querySelectorAll('h3, [class*=CardTitle]')).map(c=>(c.textContent||'').trim()).filter(Boolean);
  `)));
  ws.close();
})();
