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
  // 开图片、关文字 → 纯图片处理
  console.log("切设置:", await ev(`
    const m=document.querySelector('main');
    const byLabel=n=>{const l=Array.from(m.querySelectorAll('label')).find(x=>((x.innerText||'').trim()===n)); return l?l.querySelector('input'):null;};
    const txt=byLabel('文字'); if(txt&&txt.checked) txt.click();
    const img=byLabel('图片'); if(img&&!img.checked) img.click();
    return {文字:byLabel('文字')?.checked, 图片:byLabel('图片')?.checked};
  `));
  await wait(2500);
  // 主图数量调到 2 减少耗时
  console.log("开始:", await ev(`
    const m=document.querySelector('main');
    const b=Array.from(m.querySelectorAll('button')).find(x=>/开始处理/.test(x.textContent||''));
    if(!b) return 'no btn'; if(b.disabled) return 'disabled: '+b.title; b.click(); return 'ok';
  `));
  await wait(60000);
  console.log("结果:", JSON.stringify(await ev(`
    const m=document.querySelector('main');
    const t=m.innerText||'';
    const i=t.indexOf('商品设置与文字');
    return {片段: i<0?'无':t.slice(i, i+230)};
  `), null, 1));
  ws.close(); process.exit(0);
})();
