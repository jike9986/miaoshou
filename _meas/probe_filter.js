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
  const pos = await ev(`
    const inp=Array.from(document.querySelectorAll('main input')).find(x=>/搜商品/.test(x.getAttribute('placeholder')||''));
    if(!inp) return null;
    const r=inp.getBoundingClientRect();
    return {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)};
  `);
  // 真实鼠标点进输入框
  await send("Input.dispatchMouseEvent", {type:"mousePressed", x:pos.x, y:pos.y, button:"left", clickCount:1});
  await send("Input.dispatchMouseEvent", {type:"mouseReleased", x:pos.x, y:pos.y, button:"left", clickCount:1});
  await wait(500);
  await send("Input.insertText", {text:"Yoga"});
  await wait(1500);
  console.log("搜 Yoga 后:", JSON.stringify(await ev(`
    const rows=Array.from(document.querySelectorAll('main tbody tr'));
    return {行数:rows.length, 内容:rows.map(r=>(r.innerText||'').replace(/\\s+/g,' ').slice(0,60))};
  `)));
  // 换关键词测不匹配
  await ev(`
    const inp=Array.from(document.querySelectorAll('main input')).find(x=>/搜商品/.test(x.getAttribute('placeholder')||''));
    if(inp){ const setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
      setter.call(inp,'zzzz'); inp.dispatchEvent(new Event('input',{bubbles:true})); }
    return 1;
  `);
  await wait(1500);
  console.log("搜 zzzz 后:", JSON.stringify(await ev(`
    const main=document.querySelector('main');
    return {行数:main.querySelectorAll('tbody tr').length, 空提示: /没有匹配/.test(main.innerText||'')};
  `)));
  ws.close(); process.exit(0);
})();
