# -*- coding: utf-8 -*-
"""过滑块 + 用成品主图做 1688 图搜，验证能拿到 offer。"""
import asyncio, json, urllib.parse
from playwright.async_api import async_playwright
from slide_solver import solve_slider

PROFILE = r"D:\缓存文件夹\codex工作台\p006\miaoshou\.1688-profile"
IMG = "https://aohuang888.com/tk-product-automation/products/store-8c84ab1ffd749f700e53d39eec88fee7.jpg"

async def search_by_image(page, img_url, tag=""):
    url = ("https://s.1688.com/youyuan/index.htm?tab=imageSearch&imageAddress="
           + urllib.parse.quote(img_url, safe=""))
    await page.goto(url, timeout=90000, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    # 如果落进滑块就过一次
    if "punish" in page.url or "x5sec" in page.url:
        print(f"  [{tag}] 遇到滑块，尝试自动拖动")
        ok = await solve_slider(page)
        print(f"  [{tag}] 拖动完成 ok={ok}, URL={page.url[:80]}")
        await asyncio.sleep(2)
        # 滑块过了通常会跳回目标页
        if "punish" in page.url:
            await page.goto(url, timeout=90000, wait_until="domcontentloaded")
            await asyncio.sleep(4)

    # 轮询结果
    for i in range(20):
        links = await page.eval_on_selector_all(
            "a[href*='detail.1688.com/offer/']",
            "els => [...new Set(els.map(e=>e.getAttribute('href')))].slice(0,15)")
        if links:
            return links
        if "punish" not in page.url and i == 6:
            # 再试一次滑块
            await solve_slider(page)
        await asyncio.sleep(2)
    return []

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE, headless=False, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1500, "height": 950}, locale="zh-CN")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};")
        print("1) 预热首页")
        await page.goto("https://www.1688.com/", timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        print("   ", (await page.title())[:50])

        for n, img in enumerate([IMG,
                                 "https://aohuang888.com/tk-product-automation/products/store-9df9b1aee2e7a4526dccc2bc38a20074.jpg"], 1):
            print(f"2.{n}) 图搜")
            links = await search_by_image(page, img, f"图{n}")
            print(f"   -> {len(links)} 条")
            for l in links[:6]:
                print("      ", l)
            await page.screenshot(path=f".img6_{n}.png")
        await ctx.close()

asyncio.run(main())
