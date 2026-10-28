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
  // 找所有「试算」按钮，看都在哪
  console.log(JSON.stringify(await ev(`
    return Array.from(document.querySelectorAll('button'))
      .filter(b => /试算/.test(b.textContent||''))
      .map(b => ({
        text: b.textContent.trim(),
        inDialog: !!b.closest('[role=dialog]'),
        disabled: b.disabled,
        cls: (b.className||'').slice(0,60)
      }));
  `), null, 2));
  // role=dialog 元素有几个
  console.log("dialog 数量:", await ev("return document.querySelectorAll('[role=dialog]').length"));
  console.log("dialog 的 outerHTML 头部:", await ev(`
    const d=document.querySelector('[role=dialog]');
    return d ? d.outerHTML.slice(0,300) : 'none';
  `));
  ws.close();
})();
