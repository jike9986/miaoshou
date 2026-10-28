// 用 CDP 截真实画面 + 查数字输入框实现方式
const fs = require("node:fs");
const http = require("node:http");

function getJson(p) {
  return new Promise((resolve, reject) => {
    http
      .get({ host: "127.0.0.1", port: 9333, path: p }, (res) => {
        let b = "";
        res.on("data", (c) => (b += c));
        res.on("end", () => {
          try { resolve(JSON.parse(b)); } catch (e) { reject(e); }
        });
      })
      .on("error", reject);
  });
}

(async () => {
  const list = await getJson("/json/list");
  const page = list.find((t) => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const send = (method, params) =>
    new Promise((resolve, reject) => {
      const mid = ++id;
      pending.set(mid, { resolve, reject });
      ws.send(JSON.stringify({ id: mid, method, params }));
    });
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      if (m.error) reject(new Error(JSON.stringify(m.error)));
      else resolve(m.result);
    }
  });
  await new Promise((r) => ws.addEventListener("open", r));

  // 1) 滚动到处理设置，确保可见
  await send("Runtime.evaluate", {
    expression: `document.querySelector('main, #app-content')?.scrollTo?.(0, 99999); document.querySelector('section[aria-label="选择要处理的商品"]')?.parentElement?.querySelectorAll('div')?.forEach(()=>{}); window.scrollTo(0,0);`,
  });
  await new Promise((r) => setTimeout(r, 800));

  // 2) 查所有 input 的真实 type
  const detail = await send("Runtime.evaluate", {
    expression: `(() => {
      const inputs = [...document.querySelectorAll('input')];
      return {
        total: inputs.length,
        types: [...new Set(inputs.map(i => i.type))],
        samples: inputs.slice(0, 12).map(i => ({
          type: i.type, value: i.value,
          label: i.closest('label')?.innerText?.trim().split('\\n')[0]?.slice(0,10) || ''
        })),
      };
    })()`,
    returnByValue: true,
  });
  fs.writeFileSync("C:\\AohuangDesktop\\_inputs.json", JSON.stringify(detail.result.value, null, 2), "utf-8");

  // 3) 真实截图
  const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  fs.writeFileSync("E:\\codex工作台\\p006\\tk-desktop\\docs\\shot-app.png", Buffer.from(shot.data, "base64"));

  ws.close();
  process.exit(0);
})().catch((e) => {
  fs.writeFileSync("C:\\AohuangDesktop\\_inputs.json", JSON.stringify({ error: String(e) }, null, 2), "utf-8");
  process.exit(1);
});
