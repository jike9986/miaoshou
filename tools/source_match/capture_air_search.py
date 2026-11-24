# -*- coding: utf-8 -*-
"""抓 1688 新版图搜页（air.1688.com/kapp/1688-search/pc-image-search）的数据接口。

登录态已验证有效（__cn_logon__=true，页面显示会员名）。这个页面是 React 应用，
商品列表走 XHR，所以要从 network 里捞。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
COOKIE_FILE = HERE / ".1688-cookie.txt"
IMG = ("https://aohuang888.com/tk-product-automation/products/"
       "store-8c84ab1ffd749f700e53d39eec88fee7.jpg")
OFFER_RE = re.compile(r"offer[/_]?(\d{11,14})")
OFFER_URL = re.compile(r"detail\.1688\.com/offer/(\d{11,14})")


def parse_cookie(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    out = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        out.append({"name": name.strip(), "value": value.strip(),
                    "domain": ".1688.com", "path": "/"})
    return out


async def main() -> int:
    cookies = parse_cookie(COOKIE_FILE.read_text(encoding="utf-8"))
    captured: list[dict] = []
    seen_urls: set[str] = set()

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

        async def on_resp(resp):
            u = resp.url
            if u in seen_urls:
                return
            if not any(k in u for k in ("1688.com", "alicdn.com")):
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if not any(k in ct for k in ("json", "javascript", "text/plain")):
                return
            try:
                body = await resp.text()
            except Exception:
                return
            if len(body) < 200:
                return
            hits = sorted({m.group(1) for m in OFFER_RE.finditer(body)})
            if not hits and "image" not in u.lower() and "search" not in u.lower():
                return
            seen_urls.add(u)
            captured.append({"url": u[:300], "len": len(body),
                             "offers": hits[:20], "body": body})

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))

        print("[1] 打开图搜页（URL 传图）")
        url = ("https://air.1688.com/kapp/1688-search/pc-image-search/"
               f"?tab=imageSearch&imageAddress={quote(IMG, safe='')}")
        await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(8)
        print("    URL:", page.url[:120])

        # 等结果卡片
        for i in range(12):
            cards = await page.query_selector_all(
                "[class*=offer], [class*=card], [class*=item], a[href*='offer']")
            if cards:
                print(f"    第 {i*2}s 出现 {len(cards)} 个候选元素")
                break
            await asyncio.sleep(2)

        # 滚动触发懒加载
        for _ in range(4):
            await page.mouse.wheel(0, 1200)
            await asyncio.sleep(2)

        html = await page.content()
        ids = sorted({m.group(1) for m in OFFER_RE.finditer(html)})
        hids = sorted({m.group(1) for m in OFFER_URL.finditer(html)})
        print(f"    HTML {len(html)} 字节 | offer(松) {len(ids)} | offer(URL) {len(hids)}")
        print(f"    样例: {hids[:6] or ids[:6]}")

        await page.screenshot(path=str(HERE / ".air_search.png"))

        print(f"\n[2] 捕获 {len(captured)} 条相关响应")
        captured.sort(key=lambda x: -len(x["offers"]))
        for c in captured[:15]:
            print(f"    {c['len']:>7}B offers={len(c['offers']):>3} {c['url'][:110]}")
            if c["offers"]:
                print(f"           {c['offers'][:8]}")

        # 存最大的几条
        for i, c in enumerate(captured[:5]):
            (HERE / f".air_resp_{i}.txt").write_text(c["body"], encoding="utf-8")

        # 也把全部响应 URL 存下来方便排查
        (HERE / ".air_resp_index.json").write_text(
            json.dumps([{"url": c["url"], "len": c["len"],
                         "offers": c["offers"]} for c in captured],
                       ensure_ascii=False, indent=1), encoding="utf-8")

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))