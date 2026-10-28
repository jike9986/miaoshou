// 需要妙手连接的页面在「已连接」态下的验收（用拦截 fetch 伪造店铺/采集箱/定时任务，
// 因为本机没有真实妙手凭据）。覆盖：
//  • 采集箱：列表渲染、详情抽屉、单条认领、批量认领
//  • 定时发布：列表、新建抽屉校验、启停开关、删除确认、立即执行
//  • 发布记录：列表、刷新回执
//  • 仓库：发布弹窗（三重保险）
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
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
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
}

async function open(url) {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
  const target = await res.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  const cdp = new Cdp(ws);
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
  await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: MOCK_SOURCE });
  await cdp.send("Page.navigate", { url: `${BASE}${url}` });
  return { target, cdp };
}

async function evaluate(cdp, expression) {
  const res = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (res.exceptionDetails) {
    throw new Error(`${res.exceptionDetails.text} :: ${res.exceptionDetails.exception?.description ?? ""}`);
  }
  const v = res.result.value;
  if (typeof v === "string") {
    try {
      return JSON.parse(v);
    } catch {
      return v;
    }
  }
  return v;
}

async function shoot(cdp, name) {
  const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, `${name}.png`), Buffer.from(shot.data, "base64"));
}

// 在页面里伪造后端响应（只拦 /api/v1，其余放行）
const MOCK_SOURCE = `
(() => {
  const real = window.fetch.bind(window);
  const json = (data, status = 200) =>
    new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
  const SHOPS = [
    { shopId: '18077147', name: 'LuoMan Store', site: 'MY', siteName: '马来西亚', languages: ['ms','en'], status: '1' },
    { shopId: '18077152', name: 'LuoMan MY 2', site: 'MY', siteName: '马来西亚', languages: ['ms','en'], status: '1' }
  ];
  const TASKS = [
    { id: 'task-aaa111', name: '罗蔓 每天 09:30', enabled: true, accountId: '1', account: '妙手罗蔓',
      shopId: '18077147', shopName: 'LuoMan Store', template: '马来', frequency: 'daily', days: [],
      dayOfMonth: [], startDate: '', endDate: '', time: '09:30', perRun: 10, claimOnly: false,
      lastRunDate: '2026-10-26', attemptDate: '2026-10-26', attempts: 0, nextDueAt: 1793100000,
      nextDueText: '2026-10-28 09:30', createdAt: 1793000000, updatedAt: 1793000000,
      lastResult: { status: 'ok', count: 10, failed: 0, at: 1793000000 } },
    { id: 'task-bbb222', name: '蒋新宇 每周一 21:00', enabled: false, accountId: '1', account: '妙手罗蔓',
      shopId: '18077152', shopName: 'LuoMan MY 2', template: '马来', frequency: 'weekly', days: [1,4],
      dayOfMonth: [], startDate: '', endDate: '', time: '21:00', perRun: 20, claimOnly: true,
      lastRunDate: '', attemptDate: '', attempts: 0, nextDueAt: null,
      nextDueText: '已停用', createdAt: 1793000000, updatedAt: 1793000000, lastResult: {} }
  ];
  const COLLECT = {
    items: [
      { key: '778812:18077147', detailId: '778812', shopId: '18077147', shopName: 'LuoMan Store', site: 'MY',
        title: 'Women Running Shoes Breathable Mesh Sneakers', imageUrl: '', sourceId: '681234567890',
        status: '未发布', editModel: 'shop', inWarehouse: false, hasDraftIn: [] },
      { key: '778813:18077147', detailId: '778813', shopId: '18077147', shopName: 'LuoMan Store', site: 'MY',
        title: 'Kasut Sukan Wanita', imageUrl: '', sourceId: null,
        status: '未发布', editModel: 'shop', inWarehouse: false, hasDraftIn: ['妙手罗蔓'] }
    ],
    page: 1, pageSize: 20, total: 2, hasMore: false, scannedAt: 1793074944, degraded: false
  };
  const LOGS = {
    items: [
      { logId: '521', detailId: '800901', warehouseId: '1042', warehouseKey: '778812:18077147',
        shopId: '18077152', shopName: 'LuoMan MY 2', title: 'Kasut Sukan Wanita Breathable Mesh Sneakers',
        status: 'success', reason: '', mode: 'carrier', batchId: 'pub_test', scheduleId: null,
        accountId: '1', platformItemId: '1729384756',
        itemEditUrl: 'https://seller.tiktokglobalshop.com/product/1729384756', attempts: 1,
        requestedAt: 1793000000, finishedAt: 1793000060 },
      { logId: '522', detailId: '800902', warehouseId: null, warehouseKey: '',
        shopId: '18077147', shopName: 'LuoMan Store', title: 'Kasut Sukan Wanita',
        status: 'processing', reason: '已提交，平台尚未返回结果', mode: 'direct', batchId: 'pub_test',
        scheduleId: null, accountId: '1', platformItemId: null, itemEditUrl: null, attempts: 1,
        requestedAt: 1793000100, finishedAt: null }
    ],
    page: 1, pageSize: 20, total: 2, hasMore: false, benchOnly: true, platformTotal: 483, degraded: false
  };
  const AUTH = {
    connected: true, accountId: '1', connectionName: '妙手罗蔓', appIdHint: '…4e',
    readOnly: false, publishEnabled: true,
    accounts: [{ id: '1', name: '妙手罗蔓', appIdHint: '…4e', site: 'MY', isDefault: true,
                 status: 'active', lastVerifiedAt: 1793000000 }],
    languages: ['ms','en','th','vi','zh'], serverVersion: '1.0.0', serverName: 'aohuang-listing-server',
    dataDir: 'C:/data', limits: { mainLimit: 9, detailLimit: 9, detailHardLimit: 30, concurrency: 3, collectBoxMaxItems: 100 }
  };
  window.__log = [];
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    if (!url.includes('/api/v1/')) return real(input, init);
    const method = (init && init.method) || 'GET';
    const path = url.split('/api/v1')[1];
    window.__log.push(method + ' ' + path);
    if (path.startsWith('/auth/status')) return json(AUTH);
    if (path.startsWith('/shops')) return json({ accountId: '1', accountName: '妙手罗蔓', shops: SHOPS,
      siteLanguages: { MY: ['ms','en'] }, siteNames: { MY: '马来西亚' }, degraded: false });
    if (path.startsWith('/system/templates')) return json({ templates: [{ name: '马来', template: {} }], total: 1, selected: {} });
    if (path.startsWith('/collect-box') && path.includes('/claim')) return json({ claimed: true, detailId: '778812',
      fromShopId: '18077147', targetShopId: '18077152', mode: 'carrier', count: 1, detailIds: ['800901'],
      failures: [], collected: {} });
    if (path.startsWith('/collect-box/778812')) return json({ detailId: '778812', shopId: '18077147', mode: 'shop',
      ossMd5: 'abc', editModel: 'shop', title: 'Women Running Shoes', site: 'MY', cid: '601234',
      categoryPath: '女鞋/运动鞋/跑步鞋', language: 'zh', images: [], imageCount: 0, detailImages: [],
      skuCount: 2, skuProperties: [{ attrName: '颜色', attrValueList: [{ attrValueId: 'c1', attrValue: '黑色', imgUrl: null }] }],
      skus: [{ skuKey: ';39;c1;', price: '35.90', priceIncludeVat: '39.49', stock: 120, weight: '0.65', isDelete: '0' }],
      purchasePrice: { min: '35.90', max: '35.90', currency: 'CNY' }, weight: '0.65',
      packageDimensions: null, sizeChartUrl: null, notes: null,
      diagnostics: [{ severity: 'warning', code: 'title_too_short', message: '清理后标题长度为 22，不符合 25 到 255 字符' }],
      readyToProcess: false, inWarehouse: false, warehouseId: null });
    if (path.startsWith('/collect-box')) return json(COLLECT);
    if (path.startsWith('/schedules') && method === 'GET') return json({ items: TASKS, total: TASKS.length, serverTime: 1793075000 });
    if (path.startsWith('/schedules') && method === 'POST' && path.includes('/run')) return json({
      taskId: 'task-aaa111', status: 'short', requested: 10, count: 7, failed: 1,
      reason: '实际只处理了 7 件，少于计划的 10 件',
      results: [{ detailId: '800901', status: 'published', title: 'Kasut Sukan Wanita' }], at: 1793075100 });
    if (path.startsWith('/schedules') && method === 'POST') return json({ task: TASKS[0] }, 201);
    if (path.startsWith('/schedules') && method === 'PATCH') return json({ task: TASKS[0] });
    if (path.startsWith('/schedules') && method === 'DELETE') return json({ deleted: true, id: 'task-aaa111', logsKept: 37 });
    if (path.startsWith('/publish/logs/refresh')) return json({ updated: 1, degraded: false, accountId: '1' });
    if (path.startsWith('/publish/logs')) return json(LOGS);
    if (path.startsWith('/groups')) return json({ groups: [{ id: '3', name: '精品', color: 'amber', sort: 0, count: 28, createdAt: 1, updatedAt: 1 }], items: [], total: 1 });
    return json({ error: { code: 'NOT_FOUND', message: 'mock 未覆盖：' + path, details: {}, requestId: 'mock' } }, 404);
  };
})();
`;

const results = {};
async function step(name, fn) {
  try {
    results[name] = await fn();
  } catch (error) {
    results[name] = { error: String(error).slice(0, 300) };
  }
}

/* ---------------- 采集箱 ---------------- */
{
  const { target, cdp } = await open("/collect");
  await sleep(3500);

  await step("collectList", async () =>
    evaluate(
      cdp,
      `JSON.stringify({
        rows: document.querySelectorAll('tbody tr').length,
        firstRow: document.querySelector('tbody tr')?.innerText.replace(/\\n/g,' · '),
        headline: document.querySelector('h1')?.textContent
      })`
    )
  );

  await step("collectDetail", async () =>
    evaluate(
      cdp,
      `(async () => {
         const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '详情');
         btn.click();
         await new Promise(r => setTimeout(r, 1500));
         const drawer = document.querySelector('[role=dialog]');
         return {
           open: Boolean(drawer),
           title: drawer?.querySelector('h2')?.textContent,
           text: drawer?.innerText.replace(/\\n+/g,' | ').slice(0, 400)
         };
       })()`
    )
  );
  await shoot(cdp, "collect-detail");

  await step("collectClaim", async () =>
    evaluate(
      cdp,
      `(async () => {
         const drawer = document.querySelector('[role=dialog]');
         const claim = [...drawer.querySelectorAll('button')].find(b => b.textContent.trim() === '认领到店铺');
         claim.click();
         await new Promise(r => setTimeout(r, 2000));
         return {
           drawerOpen: Boolean(document.querySelector('[role=dialog]')),
           toast: document.querySelector('[data-sonner-toast]')?.innerText.replace(/\\n/g,' | ') ?? null,
           requests: window.__log.filter(u => u.includes('claim'))
         };
       })()`
    )
  );

  await step("collectBatch", async () =>
    evaluate(
      cdp,
      `(async () => {
         const boxes = [...document.querySelectorAll('tbody input[type=checkbox]')];
         boxes.slice(0,2).forEach(b => b.click());
         await new Promise(r => setTimeout(r, 600));
         const bar = document.querySelector('[role=toolbar]');
         const btn = [...document.querySelectorAll('button')].find(b => b.textContent.includes('批量认领'));
         btn.click();
         await new Promise(r => setTimeout(r, 900));
         const dialog = document.querySelector('[role=dialog]');
         const confirm = [...dialog.querySelectorAll('button')].find(b => b.textContent.includes('认领 2 件'));
         confirm.click();
         await new Promise(r => setTimeout(r, 2000));
         return {
           bar: bar ? bar.innerText.replace(/\\n/g,' | ') : null,
           toast: document.querySelector('[data-sonner-toast]')?.innerText.replace(/\\n/g,' | ') ?? null,
           requests: window.__log.filter(u => u.includes('claim'))
         };
       })()`
    )
  );
  await shoot(cdp, "collect-batch");
  await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`);
}

/* ---------------- 定时发布 ---------------- */
{
  const { target, cdp } = await open("/schedules");
  await sleep(3500);

  await step("schedulesList", async () =>
    evaluate(
      cdp,
      `JSON.stringify({
        rows: document.querySelectorAll('tbody tr').length,
        text: document.querySelector('tbody')?.innerText.replace(/\\n+/g,' | ').slice(0, 400)
      })`
    )
  );
  await shoot(cdp, "schedules-list");

  await step("scheduleCreateValidate", async () =>
    evaluate(
      cdp,
      `(async () => {
         const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '新建任务');
         btn.click();
         await new Promise(r => setTimeout(r, 900));
         const drawer = document.querySelector('[role=dialog]');
         const timeInput = drawer.querySelector('input[placeholder="09:30"]');
         const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
         setter.call(timeInput, '99:99');
         timeInput.dispatchEvent(new Event('input', { bubbles: true }));
         await new Promise(r => setTimeout(r, 300));
         const submit = [...drawer.querySelectorAll('button')].find(b => b.textContent.trim() === '创建任务');
         submit.click();
         await new Promise(r => setTimeout(r, 700));
         return {
           open: Boolean(document.querySelector('[role=dialog]')),
           errors: [...document.querySelectorAll('[role=alert]')].map(e => e.textContent.trim()).slice(0,4),
           posted: window.__log.filter(u => u.startsWith('POST /schedules'))
         };
       })()`
    )
  );
  await shoot(cdp, "schedules-validation");

  await step("scheduleCreateOk", async () =>
    evaluate(
      cdp,
      `(async () => {
         const drawer = document.querySelector('[role=dialog]');
         const timeInput = drawer.querySelector('input[placeholder="09:30"]');
         const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
         setter.call(timeInput, '21:15');
         timeInput.dispatchEvent(new Event('input', { bubbles: true }));
         await new Promise(r => setTimeout(r, 300));
         const submit = [...drawer.querySelectorAll('button')].find(b => b.textContent.trim() === '创建任务');
         submit.click();
         await new Promise(r => setTimeout(r, 1600));
         return {
           open: Boolean(document.querySelector('[role=dialog]')),
           toast: document.querySelector('[data-sonner-toast]')?.innerText.replace(/\\n/g,' | ') ?? null,
           posted: window.__log.filter(u => u.startsWith('POST /schedules'))
         };
       })()`
    )
  );

  await step("scheduleToggle", async () =>
    evaluate(
      cdp,
      `(async () => {
         const sw = document.querySelector('tbody input[role=switch]');
         sw.click();
         await new Promise(r => setTimeout(r, 1200));
         return {
           patched: window.__log.filter(u => u.startsWith('PATCH /schedules')),
           toast: document.querySelector('[data-sonner-toast]')?.innerText.replace(/\\n/g,' | ') ?? null
         };
       })()`
    )
  );

  await step("scheduleRun", async () =>
    evaluate(
      cdp,
      `(async () => {
         const runBtn = [...document.querySelectorAll('button[aria-label^="立即执行"]')][0];
         if (!runBtn) return { error: 'no-run-button', labels: [...document.querySelectorAll('button')].map(b=>b.getAttribute('aria-label')).filter(Boolean) };
         runBtn.click();
         await new Promise(r => setTimeout(r, 800));
         const dialog = document.querySelector('[role=dialog]');
         const confirm = [...dialog.querySelectorAll('button')].find(b => b.textContent.trim() === '立即执行');
         confirm.click();
         await new Promise(r => setTimeout(r, 2000));
         return {
           toast: document.querySelector('[data-sonner-toast]')?.innerText.replace(/\\n/g,' | ') ?? null,
           runs: window.__log.filter(u => u.includes('/run'))
         };
       })()`
    )
  );

  await step("scheduleDelete", async () =>
    evaluate(
      cdp,
      `(async () => {
         const del = [...document.querySelectorAll('button[aria-label^="删除 "]')][0];
         del.click();
         await new Promise(r => setTimeout(r, 800));
         const dialog = document.querySelector('[role=dialog]');
         const confirm = [...dialog.querySelectorAll('button')].find(b => b.textContent.trim() === '删除任务');
         confirm.click();
         await new Promise(r => setTimeout(r, 1500));
         return {
           toast: document.querySelector('[data-sonner-toast]')?.innerText.replace(/\\n/g,' | ') ?? null,
           deletes: window.__log.filter(u => u.startsWith('DELETE /schedules'))
         };
       })()`
    )
  );
  await shoot(cdp, "schedules-after");
  await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`);
}

/* ---------------- 发布记录 ---------------- */
{
  const { target, cdp } = await open("/publish");
  await sleep(3500);
  await step("publishLogs", async () =>
    evaluate(
      cdp,
      `JSON.stringify({
        rows: document.querySelectorAll('tbody tr').length,
        text: document.querySelector('tbody')?.innerText.replace(/\\n+/g,' | ').slice(0, 500)
      })`
    )
  );
  await step("publishRefresh", async () =>
    evaluate(
      cdp,
      `(async () => {
         const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '刷新回执');
         btn.click();
         await new Promise(r => setTimeout(r, 1600));
         return {
           toast: document.querySelector('[data-sonner-toast]')?.innerText.replace(/\\n/g,' | ') ?? null,
           calls: window.__log.filter(u => u.includes('/publish/logs'))
         };
       })()`
    )
  );
  await shoot(cdp, "publish-logs");
  await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`);
}

fs.writeFileSync(path.join(OUT, "mock-flow.json"), JSON.stringify(results, null, 2));
console.log(JSON.stringify(results, null, 2));
process.exit(0);
