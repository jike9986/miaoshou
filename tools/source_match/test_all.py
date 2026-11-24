# -*- coding: utf-8 -*-
"""带真实登录态，逐个测 1688 的搜索入口，看哪个能拿到真商品。

跑四个：
  1. 旧版图搜 s.1688.com/youyuan/  (imageAddress 传 URL)
  2. 新版 air.1688.com 图搜（上传）
  3. 关键词搜索 s.1688.com/selloffer/offer_search.html
  4. 图搜的旧 json 接口 s.1688.com/youyuan/imageSearch.json
判定标准：页面/响应里出现**真实的** detail.1688.com/offer/<id>.html 链接。
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import quote

import requests
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
COOKIE_FILE = HERE / ".1688-cookie.txt"
IMG = ("https://aohuang888.com/tk-product-automation/products/"
       "store-8c84ab1ffd749f700e53d39eec88fee7.jpg")
# 只认真实 URL 形式的 offer
REAL = re.compile(r"detail\.1688\.com/offer/(\d{9,16})\.html")


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


async def scan(page, tag: str) -> tuple[int, list[str]]:
    """等结果，从 DOM 和 HTML 里抽真 offer。"""
    best: list[str] = []
    for i in range(18):
        await asyncio.sleep(2)
        try:
            hrefs = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)")
        except Exception:
            hrefs = []
        ids = sorted({m.group(1) for h in hrefs
                      for m in [REAL.search(h)] if m})
        if len(ids) > len(best):
            best = ids
        if len(ids) >= 3:
            print(f"    [{tag}] 第 {i*2}s 拿到 {len(ids)} 个真 offer")
            return len(ids), ids
        await page.mouse.wheel(0, 900)
    # 兜底：扫整页 HTML
    try:
        html = await page.content()
        ids2 = sorted({m.group(1) for m in REAL.finditer(html)})
        if len(ids2) > len(best):
            best = ids2
    except Exception:
        pass
    print(f"    [{tag}] 最终 {len(best)} 个")
    return len(best), best


async def main() -> int:
    cookies = parse_cookie(COOKIE_FILE.read_text(encoding="utf-8"))
    local = HERE / ".upload.jpg"
    if not local.exists():
        local.write_bytes(requests.get(IMG, timeout=45).content)

    results = {}
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

        # --- 1 旧版图搜 ---
        print("\n[1] 旧版图搜 s.1688.com/youyuan")
        try:
            u = ("https://s.1688.com/youyuan/index.htm?tab=imageSearch"
                 f"&imageAddress={quote(IMG, safe='')}")
            await page.goto(u, wait_until="domcontentloaded", timeout=90_000)
            await asyncio.sleep(4)
            print(f"    URL: {page.url[:110]}")
            blocked = any(k in page.url for k in ("punish", "x5sec", "login"))
            print(f"    被拦: {blocked}")
            n, ids = await scan(page, "youyuan")
            results["youyuan"] = {"blocked": blocked, "n": n, "ids": ids}
        except Exception as exc:
            print(f"    失败 {type(exc).__name__}: {str(exc)[:90]}")
            results["youyuan"] = {"error": str(exc)[:90]}

        # --- 2 关键词搜索 ---
        print("\n[2] 关键词搜索")
        try:
            u = ("https://s.1688.com/selloffer/offer_search.html?keywords="
                 + quote("男士运动鞋 厚底 黑色"))
            await page.goto(u, wait_until="domcontentloaded", timeout=90_000)
            await asyncio.sleep(4)
            print(f"    URL: {page.url[:110]}")
            blocked = any(k in page.url for k in ("punish", "x5sec", "login"))
            print(f"    被拦: {blocked}")
            n, ids = await scan(page, "keyword")
            results["keyword"] = {"blocked": blocked, "n": n, "ids": ids}
        except Exception as exc:
            print(f"    失败 {type(exc).__name__}: {str(exc)[:90]}")
            results["keyword"] = {"error": str(exc)[:90]}

        # --- 3 新版 air 图搜（上传 + 等）---
        print("\n[3] 新版 air 图搜上传")
        try:
            await page.goto("https://air.1688.com/kapp/1688-search/pc-image-search/",
                            wait_until="domcontentloaded", timeout=120_000)
            await asyncio.sleep(4)
            fi = await page.query_selector("input[type=file]")
            if fi:
                await fi.set_input_files(str(local))
                await asyncio.sleep(6)
                # 尝试点真实搜索按钮（排除"热门搜索"这种）
                btns = await page.query_selector_all("button")
                for b in btns:
                    try:
                        t = (await b.inner_text()).strip()
                        if t in ("搜索", "搜 索", "以图搜货") and await b.is_visible():
                            print(f"    点按钮 {t!r}")
                            await b.click()
                            await asyncio.sleep(5)
                            break
                    except Exception:
                        continue
                n, ids = await scan(page, "air")
                results["air"] = {"n": n, "ids": ids}
            else:
                results["air"] = {"error": "no file input"}
        except Exception as exc:
            print(f"    失败 {type(exc).__name__}: {str(exc)[:90]}")
            results["air"] = {"error": str(exc)[:90]}

        await page.screenshot(path=str(HERE / ".test_all.png"))
        await browser.close()

    print("\n" + "=" * 60)
    for k, v in results.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))