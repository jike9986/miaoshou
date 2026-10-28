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
  const read = async () => ev(`
    const m=document.querySelector('main');
    const t=m.innerText||'';
    const warnings=['都关着','定价模板需要','发布已开启'].filter(w=>t.includes(w));
    const start=Array.from(m.querySelectorAll('button')).find(x=>/开始处理/.test(x.textContent||''));
    const byLabel=n=>{const l=Array.from(m.querySelectorAll('label')).find(x=>((x.innerText||'').trim()===n)); return l?l.querySelector('input').checked:null;};
    return {警告:warnings, 开始禁用:start?start.disabled:null, 开始title:start?start.title:null,
            文字:byLabel('文字'), 图片:byLabel('图片'), 定价:byLabel('定价模板'), 发布:byLabel('发布到店铺')};
  `);
  // 勾一个商品
  console.log("勾选商品:", await ev(`
    const m=document.querySelector('main');
    const boxes=Array.from(m.querySelectorAll('label')).filter(l=>/Running shoes|拖鞋/.test(l.innerText||''));
    if(!boxes.length) return 'no product';
    const inp=boxes[0].querySelector('input[type=checkbox]');
    if(inp) inp.click(); return 'ok';
  `));
  await wait(2500);
  console.log("勾选后:", JSON.stringify(await read()));

  // 关图片
  await ev(`
    const m=document.querySelector('main');
    const l=Array.from(m.querySelectorAll('label')).find(x=>((x.innerText||'').trim()==='图片'));
    const i=l&&l.querySelector('input'); if(i&&i.checked) i.click(); return 1;
  `);
  await wait(2000);
  console.log("关图片后:", JSON.stringify(await read()));

  // 再关文字 → 全关
  await ev(`
    const m=document.querySelector('main');
    const l=Array.from(m.querySelectorAll('label')).find(x=>((x.innerText||'').trim()==='文字'));
    const i=l&&l.querySelector('input'); if(i&&i.checked) i.click(); return 1;
  `);
  await wait(2000);
  console.log("全关后:", JSON.stringify(await read()));
  ws.close(); process.exit(0);
})();
