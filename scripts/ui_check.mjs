// 用 CDP 走一遍界面主路径，确认部署后的软件真的能用。
// Page.captureScreenshot 在这台机器上会卡死，所以只做文本探测 + 断言。
const CDP_PORT = 9333;

async function targets() {
  const r = await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`);
  return (await r.json()).filter((t) => t.type === "page");
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener("message", (e) => {
      const msg = JSON.parse(e.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`timeout ${method}`));
        }
      }, 30000);
    });
  }
  async evaluate(expression, isAsync = false) {
    const r = await this.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: isAsync,
    });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description || "eval error");
    }
    return r.result.value;
  }
}

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok: !!ok, detail: String(detail).slice(0, 300) });
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? `  [${String(detail).slice(0, 300)}]` : ""}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const pages = await targets();
  if (!pages.length) throw new Error("没有可用的页面目标");
  const ws = new WebSocket(pages[0].webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", rej, { once: true });
  });
  const cdp = new Cdp(ws);
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");

  // --- 1. 页面已挂载 ---
  const title = await cdp.evaluate("document.title");
  check("页面已加载", !!title, title);

  const hasRoot = await cdp.evaluate("!!document.querySelector('#root')?.children.length");
  check("React 已挂载", hasRoot);

  const text = await cdp.evaluate("document.body.innerText");
  check("界面有内容", text.length > 200, `${text.length} 字符`);

  // --- 2. 侧边栏导航（绝对不许有图片翻译 / 采集箱） ---
  const navs = await cdp.evaluate(`
    (() => Array.from(document.querySelectorAll('aside a')).map(a => ({
      text: (a.textContent || '').trim(),
      href: a.getAttribute('href') || ''
    })))()
  `);
  check("侧边栏有导航项", navs.length >= 4, navs.map((n) => n.text).join(" | "));

  const banned = navs.filter((n) => /图片翻译|采集箱|ImageTranslate|CollectBox/i.test(n.text + n.href));
  check("没有「图片翻译」和「采集箱」导航项", banned.length === 0, banned.map((n) => n.text).join(","));

  // --- 3. 标题栏品牌（用户要求标题栏不出现「奥皇上品」） ---
  const titleBar = await cdp.evaluate(`
    (() => {
      const el = document.querySelector('header') || document.querySelector('[class*="titlebar"]');
      return el ? (el.textContent || '').trim() : '';
    })()
  `);
  check("标题栏不含品牌名", !/奥皇上品/.test(titleBar), titleBar.slice(0, 80));

  // --- 4. 逐个路由走一遍，确认不白屏 ---
  // 只列 App.tsx 里真实存在的路由。旧路径（publish/collect/images/carrier）
  // 都是 Navigate 重定向，不单独测；settings 走标题栏入口，不在侧边栏。
  const routes = [
    ["/automation", "自动化上品"],
    ["/warehouse", "成品仓库"],
    ["/schedules", "定时发布"],
    ["/words", "违规词库"],
  ];
  for (const [href, label] of routes) {
    const clicked = await cdp.evaluate(`
      (() => {
        const a = Array.from(document.querySelectorAll('aside a'))
          .find(x => (x.getAttribute('href') || '').endsWith(${JSON.stringify(href)}) ||
                     (x.getAttribute('href') || '') === ${JSON.stringify("#" + href)});
        if (!a) return false;
        a.click();
        return true;
      })()
    `);
    await sleep(1200);
    const body = await cdp.evaluate("document.body.innerText");
    const hash = await cdp.evaluate("location.hash");
    const ok = clicked && body.length > 150 && !/^\s*$/.test(body);
    check(`路由 ${label} 可进入且不白屏`, ok, `hash=${hash} 文本=${body.length}字符`);
  }

  // --- 5. 仓库页：卡片只显示图 + 标题，且有分页 ---
  await cdp.evaluate(`
    (() => {
      const a = Array.from(document.querySelectorAll('aside a'))
        .find(x => (x.getAttribute('href') || '').includes('/warehouse'));
      if (a) a.click();
    })()
  `);
  await sleep(2500);
  const wh = await cdp.evaluate(`document.body.innerText`);
  check("仓库页显示商品", /件|商品/.test(wh), wh.slice(0, 120).replace(/\n/g, " "));
  // 「共 N 件」是统计卡上的总数，是允许的；用户反对的是分页器旁边重复的
  // 「共 N 条」文案。所以只挡「条」，不挡「件」。
  const noCount = !/共\s*\d+\s*条/.test(wh);
  check("分页器没有「共 N 条」", noCount);
  const imgs = await cdp.evaluate("document.querySelectorAll('img').length");
  check("仓库页有图片", imgs > 0, `${imgs} 张`);

  // --- 6. 设置页（操作员 + 审计日志），从标题栏身份按钮进 ---
  const settingsOk = await cdp.evaluate(`
    (() => {
      location.hash = "#/settings";
      return true;
    })()
  `);
  await sleep(1800);
  const st = await cdp.evaluate("document.body.innerText");
  check("设置页可进入", settingsOk && st.length > 200, `${st.length}字符`);
  check("设置页有「操作员」", /操作员/.test(st), st.slice(0, 100).replace(/\n/g, " "));
  check("设置页有「审计日志」", /审计日志/.test(st));
  check("显示当前身份", /当前身份|身份/.test(st));

  // --- 7. 后端连通性（界面内发起真实请求） ---
  const apiOk = await cdp.evaluate(`
    (async () => {
      try {
        const base = window.__DSH_BOOT__?.apiBase || window.__AOHUANG_API_BASE__ || 'http://127.0.0.1:8812';
        const r = await fetch(base + '/api/v1/system/health');
        return { status: r.status, ok: r.ok };
      } catch (e) { return { error: String(e) }; }
    })()
  `, true);
  check("界面能连上后端", apiOk && apiOk.status === 200, JSON.stringify(apiOk));

  ws.close();

  const pass = results.filter((r) => r.ok).length;
  const fail = results.length - pass;
  const fs = await import("node:fs");
  fs.writeFileSync(
    process.argv[2] || "ui_check.json",
    JSON.stringify({ total: results.length, pass, fail, cases: results }, null, 1)
  );
  console.log(`\nTOTAL ${pass} PASS ${fail} FAIL`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error("FATAL", e);
  process.exit(2);
});
