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
  await ev(`location.hash='#/warehouse'; return 1;`);
  await wait(4000);
  // 搜索框输入
  const pos = await ev(`
    const inp=document.querySelector('main input[type=search], main input[placeholder*="搜"]');
    if(!inp) return null;
    const r=inp.getBoundingClientRect();
    return {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)};
  `);
  if (!pos) { console.log("没有搜索框"); process.exit(0); }
  await send("Input.dispatchMouseEvent", {type:"mousePressed", x:pos.x, y:pos.y, button:"left", clickCount:1});
  await send("Input.dispatchMouseEvent", {type:"mouseReleased", x:pos.x, y:pos.y, button:"left", clickCount:1});
  await wait(400);
  await send("Input.insertText", {text:"3484413012"});
  await wait(4000);
  console.log("搜索结果:", await ev(`
    const arts=Array.from(document.querySelectorAll('main article'));
    return {数量:arts.length, 首个:(arts[0]?arts[0].innerText.replace(/\\s+/g,' ').slice(0,60):'无')};
  `));
  await ev(`
    const a=document.querySelector('main article');
    if(a){const b=a.querySelector('button'); if(b)b.click();} return 1;
  `);
  await wait(4500);
  console.log(JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'no dialog';
    const t=d.innerText||'';
    const i=t.indexOf('翻译检查');
    return {片段:i<0?'无':t.slice(i,i+180), 有明细:/逐字段明细/.test(t)};
  `), null, 1));
  ws.close(); process.exit(0);
})();
