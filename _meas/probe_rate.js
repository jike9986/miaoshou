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
  const ev = async (body, isAsync) => {
    const r = await send("Runtime.evaluate", { expression: (isAsync?"(async () => {":"(() => {") + `\n${body}\n})()`, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return {ERR: r.exceptionDetails.exception?.description};
    return r.result.value;
  };
  const wait = ms => new Promise(r=>setTimeout(r,ms));
  await ev(`location.hash='#/warehouse'; return 1;`);
  await wait(3000);
  // 勾一件并开发布弹窗
  await ev(`const cb=document.querySelector('main input[type=checkbox], main button[role=checkbox]'); if(cb)cb.click(); return 1;`);
  await wait(1200);
  await ev(`const b=Array.from(document.querySelectorAll('main button')).find(x=>(x.textContent||'').trim()==='发布'); if(b)b.click(); return 1;`);
  await wait(2500);
  // 直接在 React 层验证：篡改 props 里 sourceShopIds 不可行，改为检查弹窗渲染出的提示文案是否随数据变化
  console.log("当前弹窗含载体提示:", await ev(`
    const d=document.querySelector('[role=dialog]');
    return d ? /载体/.test(d.innerText||'') : 'no dialog';
  `));
  console.log("弹窗含汇率:", await ev(`
    const d=document.querySelector('[role=dialog]');
    return d ? /汇率/.test(d.innerText||'') : 'no dialog';
  `));
  // 汇率非法时提交按钮应禁用
  console.log("填非法汇率:", await ev(`
    const d=document.querySelector('[role=dialog]');
    const inp=Array.from(d.querySelectorAll('input')).find(i=>/留空用商品汇率/.test(i.placeholder||''));
    if(!inp) return 'no input';
    const setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
    setter.call(inp,'-5');
    inp.dispatchEvent(new Event('input',{bubbles:true}));
    return 'set -5';
  `));
  await wait(1200);
  console.log("提示与禁用:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    const t=d.innerText||'';
    const submit=Array.from(d.querySelectorAll('button')).find(b=>/发布 \\d+ 件/.test(b.textContent||''));
    return { 有错误提示: /汇率需大于/.test(t), 提交禁用: submit ? submit.disabled : null };
  `)));
  ws.close();
})();
