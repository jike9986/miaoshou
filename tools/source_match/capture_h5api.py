# -*- coding: utf-8 -*-
"""精确抓图搜结果接口：完整 URL、请求头、请求体，并对照页面上真看到的商品。

h5api 的 mtop 接口要 `sign` = md5(token + "&" + t + "&" + appKey + "&" + data)，
token 来自 `_m_h5_tk` cookie 的前半段。抓到一次就能脱离浏览器直接调用。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, unquote

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
COOKIE_FILE = HERE / ".1688-cookie.txt"
IMG = ("https://aohuang888.com/tk-product-automation/products/"
       "store-8c84ab1ffd749f700e53d39eec88fee7.jpg")
OFFER_URL = re.compile(r"detail\.1688\.com/offer/(\d{11,14})")


def parse_cookie(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    out = []
    for pair in raw.split(";"):
        if "=" not in pair:
            continue
        n, v = pair.split("=", 1)
        out.append({"name": n.strip(), "value": v.strip(),
                    "domain": ".1688.com", "path": "/"})
    return out


async def main() -> int:
    cookies = parse_cookie(COOKIE_FILE.read_text(encoding="utf-8"))
    calls: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
            locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900})
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        async def on_req(req):
            u = req.url
            if "h5api.m.1688.com" not in u:
                return
            entry = {"url": u, "method": req.method,
                     "headers": dict(req.headers), "post": req.post_data}
            calls.append(entry)

        page.on("request", lambda r: asyncio.create_task(on_req(r)))

        print("[1] 图搜")
        url = ("https://air.1688.com/kapp/1688-search/pc-image-search/"
               f"?tab=imageSearch&imageAddress={quote(IMG, safe='')}")
        await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        # 等接口回来
        for i in range(20):
            await asyncio.sleep(2)
            if any("recommend" in c["url"] or "image" in c["url"].lower()
                   for c in calls):
                if i > 3:
                    break

        # 从 DOM 里拿用户真正看到的商品卡片链接
        cards = await page.eval_on_selector_all(
            "a[href*='offer']", "els => els.map(e => e.href)")
        dom_ids = sorted({m.group(1) for h in cards
                          for m in [OFFER_URL.search(h)] if m})
        print(f"    DOM 卡片 offer: {len(dom_ids)} {dom_ids[:8]}")

        # 也看有没有 data-* 属性带 id
        attrs = await page.evaluate("""() => {
          const out = new Set();
          document.querySelectorAll('*').forEach(el => {
            for (const a of el.attributes || []) {
              if (/^data-/.test(a.name) && /^\\d{11,14}$/.test(a.value)) out.add(a.value);
            }
          });
          return [...out].slice(0, 40);
        }""")
        print(f"    data-* 里的长数字: {len(attrs)} {attrs[:8]}")

        print(f"\n[2] 捕获 {len(calls)} 条 h5api 调用")
        for i, c in enumerate(calls, 1):
            u = c["url"]
            name = re.search(r"mtop\.([^/]+)/", u)
            print(f"\n--- [{i}] {c['method']} {name.group(1) if name else '?'} ---")
            print(f"    URL 长度: {len(u)}")
            print(f"    前 260: {u[:260]}")
            if c.get("post"):
                print(f"    BODY: {c['post'][:300]}")

        # 存完整
        (HERE / ".h5api_calls.json").write_text(
            json.dumps(calls, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n完整调用已存 .h5api_calls.json（{len(calls)} 条）")
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))