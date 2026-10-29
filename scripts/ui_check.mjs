// 用 CDP 走一遍界面主路径，确认部署后的软件真的能用。
// Page.captureScreenshot 在这台机器上会卡死，所以只做文本探测 + 断言。
const CDP_PORT = 9333;
const BASE = "http://localhost:5173";

async function targets() {
  const r = await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`);
  return (await r.json()).filter((t) => t.type === "page");
}

async function createIsolatedPage() {
  const pages = await targets();
  const page = pages.find((item) => item.url?.includes(":5173/")) || pages[0];
  if (!page?.webSocketDebuggerUrl) throw new Error("没有可用的页面目标");
  return { page, restoreUrl: page.url };
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
  const isolated = await createIsolatedPage();
  const { page } = isolated;
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", rej, { once: true });
  });
  const cdp = new Cdp(ws);
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
  await cdp.send("Page.navigate", { url: `${BASE}/unlock` });
  await sleep(900);

  // --- 1. 页面已挂载 ---
  const title = await cdp.evaluate("document.title");
  check("页面已加载", !!title, title);

  const hasRoot = await cdp.evaluate("!!document.querySelector('#root')?.children.length");
  check("React 已挂载", hasRoot);

  const text = await cdp.evaluate("document.body.innerText");
  check("界面有内容", text.length > 120, `${text.length} 字符`);
  const hasOperatorIdentity = await cdp.evaluate(
    "Boolean(localStorage.getItem('aohuang.operator.token'))"
  );
  check("隔离运行态没有软件登录身份", !hasOperatorIdentity);

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

  // --- 4. 未登录时业务路由必须回到软件账号登录页 ---
  const guestRoutes = [
    ["/automation", "自动化上品"],
    ["/warehouse", "成品仓库"],
    ["/settings", "设置"],
  ];
  async function navigate(path) {
    await cdp.send("Page.navigate", { url: new URL(path, BASE).href });
    await sleep(900);
  }
  async function readLoginState() {
    return cdp.evaluate(`(() => {
      const visible = (element) => Boolean(element && element.getClientRects().length
        && getComputedStyle(element).visibility !== 'hidden');
      const username = document.querySelector('input[autocomplete="username"]');
      const password = document.querySelector('input[autocomplete="current-password"]');
      const loginButton = Array.from(document.querySelectorAll('button'))
        .find((button) => button.textContent.trim() === '登录');
      const main = document.querySelector('#app-content');
      return {
        path: location.pathname,
        usernameVisible: visible(username),
        passwordVisible: visible(password),
        loginVisible: visible(loginButton),
        mainText: main?.innerText || '',
        productCards: main?.querySelectorAll('article').length ?? -1,
        pageHeading: main?.querySelector('h1')?.innerText || '',
      };
    })()`);
  }
  for (const [path, label] of guestRoutes) {
    await navigate(path);
    const state = await readLoginState();
    const redirected = state.path === "/unlock";
    const loginVisible = state.usernameVisible && state.passwordVisible && state.loginVisible;
    const noBusinessContent = state.productCards === 0
      && !/自动化上品|成品仓库|仓库商品|商品详情/.test(state.mainText);
    check(
      `无身份访问 ${label} 重定向到登录页`,
      redirected,
      `path=${state.path}`
    );
    check(
      `${label} 路由显示软件登录且无商品内容`,
      loginVisible && noBusinessContent && state.pageHeading === "软件账号登录",
      `controls=${loginVisible} cards=${state.productCards} heading=${state.pageHeading}`
    );
  }

  // --- 5. 登录页视口矩阵：覆盖窗口尺寸和系统缩放 ---
  const viewportCases = [
    ...[1440, 1180, 900].flatMap((width, index) => {
      const height = [900, 720, 700][index];
      return [1, 1.25, 1.5].map((scale) => ({ width, height, scale }));
    }),
  ];
  try {
    await navigate("/unlock");
    for (const viewport of viewportCases) {
      await cdp.send("Emulation.setDeviceMetricsOverride", {
        width: viewport.width,
        height: viewport.height,
        deviceScaleFactor: viewport.scale,
        mobile: false,
      });
      await sleep(180);
      const layout = await cdp.evaluate(`(() => {
        const root = document.querySelector('#root');
        const main = document.querySelector('#app-content');
        return {
          width: innerWidth,
          height: innerHeight,
          dpr: devicePixelRatio,
          documentWidth: document.documentElement.scrollWidth,
          bodyWidth: document.body.scrollWidth,
          rootRight: root?.getBoundingClientRect().right ?? Infinity,
          mainRight: main?.getBoundingClientRect().right ?? Infinity,
        };
      })()`);
      const noHorizontalOverflow = layout.documentWidth <= layout.width
        && layout.bodyWidth <= layout.width
        && layout.rootRight <= layout.width
        && layout.mainRight <= layout.width
        && Math.abs(layout.dpr - viewport.scale) < 0.01;
      check(
        `登录页 ${viewport.width}×${viewport.height} @${viewport.scale} 无外层横溢出`,
        noHorizontalOverflow,
        JSON.stringify(layout)
      );
    }
  } finally {
    await cdp.send("Emulation.clearDeviceMetricsOverride");
  }

  // --- 8. 后端连通性（界面内发起真实请求） ---
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

  await cdp.send("Page.navigate", { url: isolated.restoreUrl });
  await sleep(200);
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
