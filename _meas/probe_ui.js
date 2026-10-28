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
  console.log(JSON.stringify(await ev(`
    const d=document.querySelector('[role=dialog]');
    if(!d) return 'no dialog';
    const t=d.innerText||'';
    return {
      识别摘要: (t.match(/(\\d+ 张有中文|没发现中文)/)||[null])[0],
      切换按钮数: Array.from(d.querySelectorAll('button')).filter(x=>/改为翻译|改为保留/.test(x.textContent||'')).length,
      有提交翻译: /提交翻译/.test(t),
      图片任务区: (t.match(/图片任务[\\s\\S]{0,160}/)||[null])[0]
    };
  `), null, 1));
  ws.close(); process.exit(0);
})();
