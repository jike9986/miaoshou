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
  // 主图数量调到 1
  console.log("主图数量:", await ev(`
    const m=document.querySelector('main');
    const inputs=Array.from(m.querySelectorAll('input[type=number]'));
    const box=inputs[0];
    if(!box) return 'no number input';
    const setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
    setter.call(box,'1');
    box.dispatchEvent(new Event('input',{bubbles:true}));
    box.dispatchEvent(new Event('blur',{bubbles:true}));
    return box.value;
  `));
  await wait(2500);
  console.log("开始:", await ev(`
    const m=document.querySelector('main');
    const b=Array.from(m.querySelectorAll('button')).find(x=>/开始处理/.test(x.textContent||''));
    if(!b) return 'no btn'; if(b.disabled) return 'disabled'; b.click(); return 'ok';
  `));
  await wait(75000);
  console.log("结果:", JSON.stringify(await ev(`
    const m=document.querySelector('main');
    const t=m.innerText||'';
    const i=t.indexOf('商品设置与文字');
    return {片段: i<0?'无':t.slice(i, i+240)};
  `), null, 1));
  ws.close(); process.exit(0);
})();
