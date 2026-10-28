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
  // 包装原 fetch 记录响应体
  await ev(`
    if (!window.__origFetch2) {
      window.__origFetch2 = window.fetch;
      window.__resp = [];
      window.fetch = function(...args) {
        const url = typeof args[0]==='string'?args[0]:(args[0]&&args[0].url);
        return window.__origFetch2.apply(this,args).then(async r => {
          if (url && url.includes('price-preview')) {
            const clone = r.clone();
            try { window.__resp.push({status: r.status, body: (await clone.text()).slice(0,400)}); } catch(e) { window.__resp.push({err:String(e)}); }
          }
          return r;
        });
      };
    }
    return 'hooked2';
  `);
  await ev(`const d=document.querySelector('[role=dialog]'); if(d){const b=Array.from(d.querySelectorAll('button')).find(x=>(x.textContent||'').trim()==='关闭'); if(b)b.click();} return true;`);
  await new Promise(r=>setTimeout(r,800));
  await ev(`const b=Array.from(document.querySelectorAll('main button')).find(x=>(x.textContent||'').includes('价格试算')); if(b)b.click(); return true;`);
  await new Promise(r=>setTimeout(r,1500));
  await ev(`window.__resp=[]; const d=document.querySelector('[role=dialog]'); const b=Array.from(d.querySelectorAll('button')).find(x=>/^试算/.test((x.textContent||'').trim())); if(b)b.click(); return true;`);
  await new Promise(r=>setTimeout(r,6000));
  console.log("响应:", JSON.stringify(await ev("return window.__resp"), null, 2));
  ws.close();
})();
