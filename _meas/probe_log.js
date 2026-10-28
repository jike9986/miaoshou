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
  console.log("到自动化:", await ev(`
    const a = Array.from(document.querySelectorAll('aside a')).find(x=>(x.textContent||'').trim()==='自动化上品');
    if (a) a.click(); return !!a;
  `));
  await wait(2500);
  console.log("有运行日志卡:", await ev(`return (document.body.innerText||'').includes('运行日志');`));
  console.log("点击展开:", await ev(`
    const btn = Array.from(document.querySelectorAll('button')).find(b=>(b.textContent||'').includes('运行日志'));
    if (!btn) return 'none';
    btn.click(); return 'clicked';
  `));
  await wait(2500);
  console.log("日志内容:", JSON.stringify(await ev(`
    const pre = document.querySelector('pre');
    if (!pre) return 'no pre';
    const t = pre.innerText||'';
    return {lines: t.split('\\n').length, sample: t.slice(0, 180)};
  `)));
  ws.close();
})();
