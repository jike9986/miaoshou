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

  // 用真实鼠标点：先取按钮中心坐标，再 Input.dispatchMouseEvent
  const box = await ev(`
    const d=document.querySelector('[role=dialog]');
    const btn=Array.from(d.querySelectorAll('button')).find(x=>/^试算/.test((x.textContent||'').trim()));
    if(!btn) return null;
    const r=btn.getBoundingClientRect();
    return {x: r.x + r.width/2, y: r.y + r.height/2};
  `);
  console.log("按钮坐标:", box);
  if (!box) { ws.close(); return; }
  await send("Input.dispatchMouseEvent", {type:"mousePressed", x:box.x, y:box.y, button:"left", clickCount:1});
  await send("Input.dispatchMouseEvent", {type:"mouseReleased", x:box.x, y:box.y, button:"left", clickCount:1});
  await wait(7000);
  console.log("结果:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'closed';
    return {tr:d.querySelectorAll('tbody tr').length, table:!!d.querySelector('table'), text:(d.innerText||'').slice(0,900)};
  `), null, 2));
  ws.close();
})();
