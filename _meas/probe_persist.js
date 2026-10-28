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
  await ev(`location.hash = '#/automation'; return 1;`);
  await wait(3000);
  console.log("存档键:", await ev(`return Object.keys(localStorage);`));
  console.log("存档内容:", JSON.stringify(await ev(`
    return localStorage.getItem('aohuang.automation.settings.v1');
  `)));
  // 改成 7 张主图，看是否落盘
  console.log("改主图数:", await ev(`
    const inp = Array.from(document.querySelectorAll('input')).find(i => i.value === '5' && i.closest('[class*=setting], section, div'));
    if (!inp) return 'no input';
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
    setter.call(inp, '7');
    inp.dispatchEvent(new Event('input', {bubbles:true}));
    inp.dispatchEvent(new Event('change', {bubbles:true}));
    return 'set 7';
  `));
  await wait(2000);
  console.log("改后存档:", JSON.stringify(await ev(`
    return localStorage.getItem('aohuang.automation.settings.v1');
  `)));
  ws.close();
})();
