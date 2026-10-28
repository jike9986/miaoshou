const http = require('http');
const get = (p) => new Promise((res, rej) => { http.get({host:'127.0.0.1',port:9333,path:p}, r => { let d=''; r.on('data',c=>d+=c); r.on('end',()=>res(JSON.parse(d))); }).on('error',rej); });
(async () => {
  const t = await get('/json/list');
  const page = t.find(x => x.type === 'page' && x.url.includes('index'));
  if (!page) { console.log(JSON.stringify({err:'no page', urls: t.map(x=>x.url)})); return; }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pend = new Map();
  const send = (method, params={}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({id:i, method, params})); });
  ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); } };
  await new Promise(r => ws.onopen = r);
  const ev = async (expr) => { const r = await send('Runtime.evaluate', {expression: expr, returnByValue: true, awaitPromise: true}); return r.result ? r.result.value : null; };
  // 点侧边栏「定时发布」
  await ev(`(function(){const a=[...document.querySelectorAll('aside a')].find(x=>x.textContent.includes('定时发布')); if(a){a.click(); return true;} return false;})()`);
  await new Promise(r => setTimeout(r, 2500));
  const tabs = await ev(`[...document.querySelectorAll('[role="tab"], button')].map(x=>x.textContent.trim()).filter(Boolean).slice(0,30)`);
  const hasHistory = await ev(`document.body.innerText.includes('执行历史')`);
  // 点执行历史
  await ev(`(function(){const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='执行历史'); if(b){b.click(); return true;} return false;})()`);
  await new Promise(r => setTimeout(r, 2200));
  const body = await ev(`document.body.innerText.slice(0,900)`);
  const hash = await ev(`location.hash`);
  console.log(JSON.stringify({hash, hasHistoryTab: hasHistory, tabs: tabs, bodyPreview: body}, null, 1));
  ws.close();
})();
