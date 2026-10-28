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

  // 勾第 2 件
  console.log("勾选:", await ev(`
    const m=document.querySelector('main');
    const lis=Array.from(m.querySelectorAll('ul > li'));
    const li=lis[1]; if(!li) return 'no li';
    const cb=li.querySelector('input[type=checkbox]');
    if(cb) cb.click();
    return (li.innerText||'').replace(/\\s+/g,' ').slice(0,40);
  `));
  await wait(2500);
  console.log("设置:", await ev(`
    const m=document.querySelector('main');
    const byLabel=n=>{const l=Array.from(m.querySelectorAll('label')).find(x=>((x.innerText||'').trim()===n)); return l?l.querySelector('input'):null;};
    const txt=byLabel('文字'); if(txt&&!txt.checked) txt.click();
    const img=byLabel('图片'); if(img&&!img.checked) img.click();
    return {文字:byLabel('文字')?.checked, 图片:byLabel('图片')?.checked};
  `));
  await wait(2500);
  console.log("开始:", await ev(`
    const m=document.querySelector('main');
    const b=Array.from(m.querySelectorAll('button')).find(x=>/开始处理/.test(x.textContent||''));
    if(!b) return 'no btn'; if(b.disabled) return 'disabled: '+b.title; b.click(); return 'ok';
  `));
  await wait(70000);
  console.log("结果:", JSON.stringify(await ev(`
    const m=document.querySelector('main');
    const t=m.innerText||'';
    const i=t.indexOf('商品设置与文字');
    const j=t.indexOf('任务摘要');
    return {进度: i<0?'无':t.slice(i, i+200), 摘要: j<0?'无':t.slice(j, j+160)};
  `), null, 1));
  ws.close(); process.exit(0);
})();
