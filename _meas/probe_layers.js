const PORT = 9333;
(async () => {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find(t => t.type === "page");
  const ws = new globalThis.WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  await new Promise(r => { ws.onopen = r; });
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); p(m.result); } };
  const send = (method, params) => new Promise(res => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({id:i, method, params: params||{}})); });
  await send("Runtime.enable"); await send("Page.enable");
  const ev = async (body) => {
    const r = await send("Runtime.evaluate", { expression: `(() => {\n${body}\n})()`, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return {ERR: r.exceptionDetails.exception?.description};
    return r.result.value;
  };
  // 等 6 秒看第一个会不会退出
  await new Promise(r=>setTimeout(r,6000));
  console.log("6秒后 dialog 数:", await ev("return document.querySelectorAll('[role=dialog]').length"));
  console.log("最上层是:", await ev(`
    const ds=document.querySelectorAll('[role=dialog]');
    const d=ds[ds.length-1];
    const r=d.getBoundingClientRect();
    return {text:(d.innerText||'').slice(0,80).replace(/\\n/g,' | '), x:Math.round(r.x), w:Math.round(r.width), h:Math.round(r.height)};
  `));
  const shot = await send("Page.captureScreenshot", { format: "png" });
  require("fs").writeFileSync("E:\\codex工作台\\p006\\tk-desktop\\docs\\shot-tpl.png", Buffer.from(shot.data, "base64"));
  console.log("截图 OK");
  ws.close();
})();
