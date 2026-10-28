const PORT = 9333;
(async () => {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find(t => t.type === "page");
  const ws = new globalThis.WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  await new Promise(r => { ws.onopen = r; });
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); p(m.result); } };
  const send = (method, params) => new Promise(res => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({id:i, method, params: params||{}})); });
  await send("Runtime.enable"); await send("Network.enable");
  const ev = async (body) => {
    const r = await send("Runtime.evaluate", { expression: `(() => {\n${body}\n})()`, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return {ERR: r.exceptionDetails.exception?.description};
    return r.result.value;
  };
  const wait = ms => new Promise(r => setTimeout(r, ms));

  // 拦截 fetch 记录
  await ev(`
    if (!window.__origFetch) {
      window.__origFetch = window.fetch;
      window.__calls = [];
      window.fetch = function(...args) {
        const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url);
        window.__calls.push({url, method: (args[1]&&args[1].method)||'GET', body: args[1]&&args[1].body});
        return window.__origFetch.apply(this, args);
      };
    }
    return 'hooked';
  `);

  // 关掉旧弹窗重新来
  await ev(`const d=document.querySelector('[role=dialog]'); if(d){const b=Array.from(d.querySelectorAll('button')).find(x=>(x.textContent||'').trim()==='关闭'); if(b)b.click();} return true;`);
  await wait(800);
  await ev(`const b=Array.from(document.querySelectorAll('main button')).find(x=>(x.textContent||'').includes('价格试算')); if(b) b.click(); return true;`);
  await wait(1500);
  console.log("弹窗标题按钮:", await ev(`const d=document.querySelector('[role=dialog]'); return d? d.querySelectorAll('button').length : 'none';`));
  await ev(`window.__calls = []; const d=document.querySelector('[role=dialog]'); const b=Array.from(d.querySelectorAll('button')).find(x=>/^试算/.test((x.textContent||'').trim())); if(b)b.click(); return true;`);
  await wait(6000);
  console.log("发出的请求:", await ev("return window.__calls"));
  console.log("表格行数:", await ev(`const d=document.querySelector('[role=dialog]'); return d? d.querySelectorAll('tbody tr').length : 'closed';`));
  ws.close();
})();
