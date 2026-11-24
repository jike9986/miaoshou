# -*- coding: utf-8 -*-
"""验证匹配准确性：把 3 件的 1688 商品标题抓出来，跟成品标题比对。"""
import asyncio, json, re, pathlib, sys
from playwright.async_api import async_playwright

HERE = pathlib.Path("tools/source_match")
rep = json.loads((HERE / ".batch-report.json").read_text(encoding="utf-8"))

def parse_cookie(raw):
    raw = raw.strip()
    out = []
    for pair in raw.split(";"):
        if "=" not in pair: continue
        n, v = pair.split("=", 1)
        out.append({"name": n.strip(), "value": v.strip(),
                    "domain": ".1688.com", "path": "/"})
    return out

async def main():
    cookies = parse_cookie((HERE / ".1688-cookie.txt").read_text(encoding="utf-8"))
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = await b.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            locale="zh-CN", viewport={"width":1440,"height":900})
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        for r in rep:
            if r.get("status") != "hit": continue
            oid = r["offerId"]
            url = f"https://detail.1688.com/offer/{oid}.html"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2.5)
                title = await page.title()
                title = title.replace("阿里巴巴", "").strip(" -")
                # 取页面上的主图
                img = await page.eval_on_selector_all(
                    "img[src*=alicdn]", "els=>els.map(e=>e.src).slice(0,2)")
                print(f"\n商品 #{r['id']}  {r['title'][:40]}")
                print(f"  1688 {oid}")
                print(f"  标题: {title[:70]}")
            except Exception as e:
                print(f"\n商品 #{r['id']} 抓取失败 {type(e).__name__}")
        await b.close()

asyncio.run(main())
