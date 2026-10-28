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
    const r = await send("Runtime.evaluate", { expression: (isAsync?"(async () => {":"(() => {")+`\n${body}\n})()`, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return {ERR: r.exceptionDetails.exception?.description};
    return r.result.value;
  };
  const wait = ms => new Promise(r=>setTimeout(r,ms));
  await ev(`location.hash='#/warehouse'; return 1;`);
  await wait(4000);
  // 打开一个有图的商品详情（点卡片里的 button）
  console.log("打开详情:", await ev(`
    const card = document.querySelector('main article');
    if (!card) return 'no card';
    const btn = card.querySelector('button');
    if (!btn) return 'no btn';
    btn.click(); return 'ok';
  `));
  await wait(3500);
  console.log("抽屉里有识别按钮:", await ev(`
    const d = document.querySelector('[role=dialog]');
    if (!d) return 'no dialog';
    const t = d.innerText || '';
    return {
      有识别按钮: /识别主图文字/.test(t),
      还有死链: /打开图片翻译/.test(t)
    };
  `));
  console.log("点识别:", await ev(`
    const d = document.querySelector('[role=dialog]');
    const b = Array.from(d.querySelectorAll('button')).find(x=>/识别主图文字/.test(x.textContent||''));
    if (!b) return 'none';
    b.click(); return 'ok';
  `));
  await wait(15000);
  console.log("识别结果:", JSON.stringify(await ev(`
    const d = document.querySelector('[role=dialog]');
    const t = d.innerText || '';
    const m = t.match(/(\\d+ 张有中文|没发现中文)/);
    return {摘要: m ? m[0] : null, 有明细: /#[123]/.test(t)};
  `)));
  ws.close();
})();
