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
  // 关弹窗
  await ev(`
    const d = document.querySelector('[role=dialog]');
    const b = d && Array.from(d.querySelectorAll('button')).find(x=>(x.textContent||'').trim()==='取消');
    if (b) b.click();
    return 1;
  `);
  await wait(1200);
  // 看顶部店铺下拉的当前值
  console.log("顶部下拉:", JSON.stringify(await ev(`
    const sel = document.querySelector('header select, header [role=combobox], select');
    if (!sel) return 'none';
    if (sel.tagName === 'SELECT') return {tag:'select', value: sel.value, opts: Array.from(sel.options).map(o=>o.textContent)};
    return {tag: sel.tagName, text: (sel.textContent||'').trim()};
  `)));
  console.log("仓库商品店铺:", JSON.stringify(await ev(`
    return Array.from(document.querySelectorAll('main article')).slice(0,2).map(a=>(a.innerText||'').slice(0,80));
  `)));
  ws.close();
})();
