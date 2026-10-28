// 交互验证脚本：游客模式进入各页 → 真实数据渲染检查 + 关键交互（搜索防抖、多选、
// 新建分组、分页、词库增删、图片翻译轮询开关）。
// 依赖一个已在 9333 端口开着的 headless Chrome 与 5173 的 Vite（见 scripts/smoke.mjs 注释）。
import fs from "node:fs";
import path from "node:path";

const PORT = 9333;
const BASE = "http://127.0.0.1:5173";
const OUT = path.resolve(".smoke");
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.handlers = new Map();
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
        return;
      }
      const list = this.handlers.get(msg.method);
      if (list) for (const fn of list) fn(msg.params);
    });
  }
  send(method, params = {}) {
    this.id += 1;
    const id = this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  on(method, fn) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(fn);
  }
}

async function openPage(url) {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(url)}`, {
    method: "PUT",
  });
  const target = await res.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  const cdp = new Cdp(ws);
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
  return { target, cdp };
}

async function evaluate(cdp, expression) {
  const res = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (res.exceptionDetails) {
    throw new Error(
      `${res.exceptionDetails.text} ${res.exceptionDetails.exception?.description ?? ""}`
    );
  }
  // 统一返回结构化值：字符串原样返回，对象直接给出（调用方无需再 JSON.parse）
  const value = res.result.value;
  if (typeof value === "string") {
    try {
      return JSON.parse(value);
    } catch {
      return value;
    }
  }
  return value;
}

/** 新标签页的 sessionStorage 是独立的：先置游客标记再导航 */
async function gotoSite(cdp, route) {
  await evaluate(cdp, `sessionStorage.setItem('aohuang.guest','1')`);
  await cdp.send("Page.navigate", { url: `${BASE}${route}` });
  await sleep(300);
}

async function shoot(cdp, name) {
  const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, `${name}.png`), Buffer.from(shot.data, "base64"));
}

const results = {};
async function step(name, fn) {
  try {
    results[name] = await fn();
  } catch (error) {
    results[name] = { error: String(error).slice(0, 400) };
  }
}

/* ---------------- 仓库：搜索 / 多选 / 分页 / 新建分组 ---------------- */
{
  const { target, cdp } = await openPage(`${BASE}/warehouse`);
  await evaluate(cdp, `sessionStorage.setItem('aohuang.guest','1')`);
  await sleep(500);
  await cdp.send("Page.navigate", { url: `${BASE}/warehouse` });
  await sleep(5200);

  results.warehouseInitial = (
    await evaluate(
      cdp,
      `JSON.stringify({
        cards: document.querySelectorAll('article').length,
        kpi: [...document.querySelectorAll('p.kpi')].length,
        total: (document.body.innerText.match(/共 [\\d,]+ 条/)||[''])[0],
        chips: [...document.querySelectorAll('button[aria-pressed]')].map(b=>b.textContent.trim()),
        brokenImgs: [...document.querySelectorAll('article img')].filter(i=>i.complete && i.naturalWidth===0).length
      })`
    )
  );

  // 搜索防抖：输入后立刻检查（不应马上变），1.2s 后应筛出结果
  results.search = (
    await evaluate(
      cdp,
      `(async () => {
        const input = document.querySelector('input[type=search]');
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        setter.call(input, 'berpinggang');
        input.dispatchEvent(new Event('input', { bubbles: true }));
        await new Promise(r => setTimeout(r, 120));
        const immediate = (document.body.innerText.match(/共 [\\d,]+ 条/)||[''])[0];
        await new Promise(r => setTimeout(r, 1400));
        const after = (document.body.innerText.match(/共 [\\d,]+ 条/)||[''])[0];
        return { immediate, after, cards: document.querySelectorAll('article').length };
      })()`
    )
  );

  // 清空搜索 → 分类 chip 点击
  results.chip = (
    await evaluate(
      cdp,
      `(async () => {
        const clear = document.querySelector('button[aria-label="清除搜索"]');
        if (clear) { clear.click(); await new Promise(r=>setTimeout(r,900)); }
        const chip = [...document.querySelectorAll('button[aria-pressed]')].find(b=>b.textContent.includes('运动鞋'));
        if (!chip) return { error: 'no-chip' };
        chip.click();
        await new Promise(r => setTimeout(r, 1400));
        return {
          total: (document.body.innerText.match(/共 [\\d,]+ 条/)||[''])[0],
          cards: document.querySelectorAll('article').length,
          filterBar: (document.body.innerText.match(/当前筛选：[^\\n]*/)||[''])[0]
        };
      })()`
    )
  );

  // 全选本页
  results.selectAll = (
    await evaluate(
      cdp,
      `(async () => {
        const btn = [...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='全选本页');
        if (!btn) return { error: 'no-select-all' };
        btn.click();
        await new Promise(r => setTimeout(r, 800));
        const bar = document.querySelector('[role=toolbar][aria-label="批量操作"]');
        return {
          bar: bar ? bar.innerText.replace(/\\n/g,' | ') : null,
          toggleLabel: [...document.querySelectorAll('button')].map(b=>b.textContent.trim()).filter(t=>t.includes('全选'))[0]
        };
      })()`
    )
  );

  // 打开「设置分类」弹窗并新建一个分组
  results.batchDialog = (
    await evaluate(
      cdp,
      `(async () => {
        const open = [...document.querySelectorAll('button')].find(b=>b.textContent.includes('设置分类'));
        if (!open) return { error: 'no-batch-button' };
        open.click();
        await new Promise(r => setTimeout(r, 900));
        const dialog = document.querySelector('[role=dialog]');
        const input = dialog?.querySelector('input[placeholder*="春季"]');
        if (!input) return { error: 'no-group-input', dialogText: dialog?.innerText.slice(0,200) };
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
        const name = '联调分类' + Math.floor(Math.random()*1000);
        setter.call(input, name);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        await new Promise(r => setTimeout(r, 200));
        const create = [...dialog.querySelectorAll('button')].find(b=>b.textContent.trim()==='创建并选中');
        if (!create) return { error: 'no-create', buttons: [...dialog.querySelectorAll('button')].map(b=>b.textContent.trim()) };
        create.click();
        await new Promise(r => setTimeout(r, 1600));
        return { name, dialogText: document.querySelector('[role=dialog]')?.innerText.slice(0, 300) };
      })()`
    )
  );
  await shoot(cdp, "warehouse-batch-dialog");

  // 应用分类
  results.applyCategory = (
    await evaluate(
      cdp,
      `(async () => {
        const btn = [...document.querySelectorAll('[role=dialog] button')].find(b=>b.textContent.includes('应用到'));
        if (!btn) return { error: 'no-apply' };
        btn.click();
        await new Promise(r => setTimeout(r, 2200));
        return {
          dialogOpen: Boolean(document.querySelector('[role=dialog]')),
          toast: document.querySelector('[data-sonner-toast]')?.innerText.replace(/\\n/g,' | ') ?? null,
          bar: document.querySelector('[role=toolbar][aria-label="批量操作"]') ? 'still' : 'cleared'
        };
      })()`
    )
  );
  await shoot(cdp, "warehouse-after-batch");
  await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`);
}

/* ---------------- 词库：搜索 / 添加 / 切换 tab ---------------- */
{
  const { target, cdp } = await openPage(`${BASE}/words`);
  await gotoSite(cdp, "/words");
  await sleep(4600);
  results.words = (
    await evaluate(
      cdp,
      `JSON.stringify({
        rows: document.querySelectorAll('tbody tr').length,
        kpi: [...document.querySelectorAll('p')].map(p=>p.textContent).filter(t=>/^[0-9,]+$/.test(t||'')).slice(0,4),
        counts: (document.body.innerText.match(/删除类 [\\d,]+ 条[^\\n]*/)||[''])[0],
        tabs: [...document.querySelectorAll('[role=tab]')].map(t=>t.textContent.trim())
      })`
    )
  );

  results.wordsAdd = (
    await evaluate(
      cdp,
      `(async () => {
        const add = [...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='添加违规词');
        if (!add) return { error: 'no-add-button', buttons: [...document.querySelectorAll('button')].map(b=>b.textContent.trim()).slice(0,20), body: document.body.innerText.slice(0,300) };
        add.click();
        await new Promise(r => setTimeout(r, 700));
        const dialog = document.querySelector('[role=dialog]');
        const area = dialog?.querySelector('textarea');
        if (!area) return { error: 'no-textarea' };
        const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
        const word = '联调词' + Math.floor(Math.random()*10000);
        setter.call(area, word);
        area.dispatchEvent(new Event('input', { bubbles: true }));
        await new Promise(r => setTimeout(r, 200));
        const submit = [...dialog.querySelectorAll('button')].find(b=>b.textContent.trim()==='添加');
        if (!submit) return { error: 'no-submit', buttons: [...dialog.querySelectorAll('button')].map(b=>b.textContent.trim()) };
        submit.click();
        await new Promise(r => setTimeout(r, 1800));
        return {
          word,
          dialogOpen: Boolean(document.querySelector('[role=dialog]')),
          toast: document.querySelector('[data-sonner-toast]')?.innerText.replace(/\\n/g,' | ') ?? null,
          firstRows: [...document.querySelectorAll('tbody tr')].slice(-2).map(r=>r.innerText.replace(/\\n/g,' · '))
        };
      })()`
    )
  );
  await shoot(cdp, "words-after-add");
  await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`);
}

/* ---------------- 其它页：渲染 + 空/引导态 ---------------- */
for (const [name, route, wait] of [
  ["collect", "/collect", 4000],
  ["schedules", "/schedules", 3200],
  ["publish", "/publish", 3200],
  ["images", "/images", 5000],
]) {
  const { target, cdp } = await openPage(`${BASE}/warehouse`);
  await gotoSite(cdp, route);
  await sleep(wait);
  results[name] = (
    await evaluate(
      cdp,
      `JSON.stringify({
        url: location.pathname,
        headline: (document.querySelector('h1')||{}).textContent,
        text: (document.body.innerText||'').replace(/\\n+/g,' | ').slice(0, 500),
        rows: document.querySelectorAll('tbody tr').length,
        buttons: [...document.querySelectorAll('button')].map(b=>b.textContent.trim()).filter(Boolean).slice(0, 16)
      })`
    )
  );
  await shoot(cdp, `${name}-guest`);
  await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`);
}

fs.writeFileSync(path.join(OUT, "flow.json"), JSON.stringify(results, null, 2));
console.log(JSON.stringify(results, null, 2));
process.exit(0);
