const fs = require("fs");
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
  const wait = ms => new Promise(r=>setTimeout(r,ms));
  await ev(`location.hash='#/warehouse'; return 1;`);
  await wait(3500);
  await ev(`const c=document.querySelector('main article'); if(c){const b=c.querySelector('button'); if(b)b.click();} return 1;`);
  await wait(3000);
  await ev(`const d=document.querySelector('[role=dialog]'); const b=d&&Array.from(d.querySelectorAll('button')).find(x=>/识别主图文字/.test(x.textContent||'')); if(b)b.click(); return 1;`);
  await wait(16000);
  const shot = await send("Page.captureScreenshot", {format:"png", captureBeyondViewport:false});
  if (shot && shot.data) {
    fs.writeFileSync("E:\\codex工作台\\p006\\tk-desktop\\docs\\shot-translate.png", Buffer.from(shot.data, "base64"));
    console.log("截图已存", shot.data.length);
  } else { console.log("截图失败"); }
  ws.close();
  process.exit(0);
})();
