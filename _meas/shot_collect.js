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
  await ev(`const a=Array.from(document.querySelectorAll('aside a')).find(x=>(x.textContent||'').trim()==='采集箱'); if(a)a.click(); return 1;`);
  await new Promise(r=>setTimeout(r,3000));
  const shot = await send("Page.captureScreenshot", { format: "png" });
  require("fs").writeFileSync("E:\\codex工作台\\p006\\tk-desktop\\docs\\shot-collect.png", Buffer.from(shot.data, "base64"));
  console.log("OK");
  ws.close();
})();
