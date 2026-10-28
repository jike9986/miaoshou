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
  console.log("点发布:", await ev(`
    const btn=Array.from(document.querySelectorAll('button')).find(b=>(b.textContent||'').trim()==='发布');
    if(!btn) return 'not found';
    btn.click(); return 'ok';
  `));
  await wait(3500);
  console.log("弹窗:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'no dialog';
    const t=d.innerText||'';
    return {有汇率:/汇率/.test(t), 有载体:/载体/.test(t)};
  `)));
  console.log("填非法汇率:", await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'no dialog';
    const inp=Array.from(d.querySelectorAll('input')).find(i=>/留空用商品汇率/.test(i.placeholder||''));
    if(!inp) return 'no input';
    const setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
    setter.call(inp,'-5');
    inp.dispatchEvent(new Event('input',{bubbles:true}));
    return 'set';
  `));
  await wait(1500);
  console.log("校验结果:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    const t=d.innerText||'';
    const submit=Array.from(d.querySelectorAll('button')).find(b=>/发布 \\d+ 件/.test(b.textContent||''));
    return {有错误:/汇率需大于/.test(t), 提交禁用: submit?submit.disabled:null};
  `)));
  ws.close();
})();
