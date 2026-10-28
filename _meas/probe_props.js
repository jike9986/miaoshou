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
  // 看按钮的 React props 里 onClick 是否挂着
  console.log(JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    const btn=Array.from(d.querySelectorAll('button')).find(x=>/^试算/.test((x.textContent||'').trim()));
    if(!btn) return 'no btn';
    const keys = Object.keys(btn).filter(k => k.startsWith('__react'));
    let props = null;
    for (const k of keys) { try { const v = btn[k]; if (v && v.memoizedProps) { props = Object.keys(v.memoizedProps); break; } } catch(e){} }
    return {reactKeys: keys, propKeys: props, disabled: btn.disabled, pointerEvents: getComputedStyle(btn).pointerEvents};
  `), null, 2));
  // 直接派发一个完整 click 序列到按钮（不经过坐标）
  console.log("dispatchEvent:", await ev(`
    const d=document.querySelector('[role=dialog]');
    const btn=Array.from(d.querySelectorAll('button')).find(x=>/^试算/.test((x.textContent||'').trim()));
    if(!btn) return 'no btn';
    btn.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
    return 'dispatched';
  `));
  await new Promise(r=>setTimeout(r,7000));
  console.log("结果:", JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    return d ? {tr:d.querySelectorAll('tbody tr').length, table:!!d.querySelector('table')} : 'closed';
  `)));
  ws.close();
})();
