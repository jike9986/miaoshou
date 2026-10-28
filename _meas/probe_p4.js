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

  // 聚焦输入框，键入，然后点页面空白处制造真实 blur
  const box = await ev(`
    const inp = Array.from(document.querySelectorAll('input')).filter(i => i.type === 'text')[0];
    if (!inp) return null;
    const r = inp.getBoundingClientRect();
    return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
  `);
  await send("Input.dispatchMouseEvent", {type:"mousePressed", x:box.x, y:box.y, button:"left", clickCount:1});
  await send("Input.dispatchMouseEvent", {type:"mouseReleased", x:box.x, y:box.y, button:"left", clickCount:1});
  await wait(300);
  await send("Input.dispatchKeyEvent", {type:"keyDown", windowsVirtualKeyCode:65, modifiers:2});
  await send("Input.dispatchKeyEvent", {type:"keyUp", windowsVirtualKeyCode:65, modifiers:2});
  await send("Input.insertText", {text:"9"});
  await wait(400);
  // 点标题区域的空白
  await send("Input.dispatchMouseEvent", {type:"mousePressed", x:800, y:120, button:"left", clickCount:1});
  await send("Input.dispatchMouseEvent", {type:"mouseReleased", x:800, y:120, button:"left", clickCount:1});
  await wait(2500);
  console.log("输入框值:", await ev(`
    const inp = Array.from(document.querySelectorAll('input')).filter(i => i.type === 'text')[0];
    return inp ? inp.value : null;
  `));
  console.log("存档:", JSON.stringify(await ev(`return localStorage.getItem('aohuang.automation.settings.v1');`)));
  ws.close();
})();
