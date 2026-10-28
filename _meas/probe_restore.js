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
  const toggle = async (name) => ev(`
    const m=document.querySelector('main');
    const l=Array.from(m.querySelectorAll('label')).find(x=>((x.innerText||'').trim()==='${name}'));
    const i=l&&l.querySelector('input'); if(!i) return 'no'; i.click(); return i.checked;
  `);
  // 恢复：关发布、开文字
  await toggle('发布到店铺');
  await wait(1500);
  await toggle('文字');
  await wait(2000);
  console.log("恢复后:", JSON.stringify(await ev(`
    const m=document.querySelector('main');
    const t=m.innerText||'';
    const byLabel=n=>{const l=Array.from(m.querySelectorAll('label')).find(x=>((x.innerText||'').trim()===n)); return l?l.querySelector('input').checked:null;};
    return {警告:['都关着','定价模板需要','发布已开启'].filter(w=>t.includes(w)),
            文字:byLabel('文字'), 图片:byLabel('图片'), 定价:byLabel('定价模板'), 发布:byLabel('发布到店铺')};
  `)));
  ws.close(); process.exit(0);
})();
