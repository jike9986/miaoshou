# -*- coding: utf-8 -*-
"""完整走一遍 1688 图搜：上传 → 等缩略图 → 点「搜索」→ 抓结果。

前一次只 set_input_files 就停了，页面还停在「上传图片 (0/6)」，
说明必须触发真正的上传+搜索按钮。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import requests
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
COOKIE_FILE = HERE / ".1688-cookie.txt"
IMG = ("https://aohuang888.com/tk-product-automation/products/"
       "store-8c84ab1ffd749f700e53d39eec88fee7.jpg")
OFFER = re.compile(r"(\d{11,14})")


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
    local = HERE / ".upload.jpg"
    if not local.exists():
        local.write_bytes(requests.get(IMG, timeout=45).content)

    hits: list[dict] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False, args=["--disable-blink-features=AutomationControlled"])
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
            if ".js" in u or ".css" in u or ".png" in u or ".jpg" in u:
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if not any(k in ct for k in ("json", "text/plain")):
                return
            try:
                body = await resp.text()
            except Exception:
                return
            if len(body) < 200:
                return
            ids = sorted({m.group(1) for m in OFFER.finditer(body)})
            # 只留「像搜索结果」的：既含 offer 又含标题/价格
            if ids and ("title" in body or "price" in body or "offerList" in body):
                hits.append({"url": u[:300], "len": len(body),
                             "ids": ids[:30], "body": body})

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))

        print("[1] 打开图搜页")
        await page.goto("https://air.1688.com/kapp/1688-search/pc-image-search/",
                        wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(5)

        print("[2] 上传")
        fi = await page.query_selector("input[type=file]")
        await fi.set_input_files(str(local))
        print("    已塞入文件，等待上传完成")
        for i in range(15):
            await asyncio.sleep(2)
            txt = await page.inner_text("body")
            m = re.search(r"上传图片\s*\((\d+)/(\d+)\)", txt)
            if m:
                print(f"    第 {i*2}s：已选 {m.group(1)}/{m.group(2)}")
                if int(m.group(1)) > 0:
                    break
            # 缩略图有 src 也算成功
            thumbs = await page.query_selector_all("[class*=thumb] img, [class*=preview] img")
            if thumbs:
                print(f"    第 {i*2}s：出现 {len(thumbs)} 个缩略图")
                break

        print("[3] 找并点「搜索」")
        clicked = False
        for sel in ["button:has-text('搜索')", "text=搜索", "[class*=search-btn]",
                    "button:has-text('以图搜货')", "button:has-text('搜 索')"]:
            try:
                els = await page.query_selector_all(sel)
                if els:
                    for e in els:
                        vis = await e.is_visible()
                        txt = (await e.inner_text()).strip()
                        if vis and "搜索" in txt:
                            print(f"    点击：{sel} -> {txt!r}")
                            await e.click()
                            clicked = True
                            break
                if clicked:
                    break
            except Exception as ex:
                print(f"    {sel} 失败 {type(ex).__name__}")

        if not clicked:
            print("    没找到搜索按钮，直接回车试试")
            await page.keyboard.press("Enter")

        # 等结果
        for i in range(25):
            await asyncio.sleep(2)
            cur = page.url
            cards = await page.eval_on_selector_all(
                "a[href*='offer']", "els => els.map(e => e.href)")
            body = await page.inner_text("body")
            empty = "空空如也" in body
            if cards or (hits and not empty):
                print(f"    第 {i*2}s 有结果：卡片 {len(cards)}，响应 {len(hits)}")
                if len(cards) > 5 or hits:
                    break
            if i % 5 == 0:
                print(f"    第 {i*2}s：卡片 {len(cards)} 空={empty} URL={cur[:70]}")

        cards = await page.eval_on_selector_all(
            "a[href*='offer']", "els => els.map(e => e.href)")
        dom_ids = sorted({m.group(1) for h in cards
                          for m in [OFFER.search(h)] if m})
        print(f"\n[4] DOM offer: {len(dom_ids)} {dom_ids[:10]}")

        hits.sort(key=lambda x: -len(x["ids"]))
        print(f"[5] 数据响应: {len(hits)}")
        for h in hits[:8]:
            print(f"    {h['len']:>8}B ids={len(h['ids']):>3} {h['url'][:95]}")
            print(f"        {h['ids'][:8]}")
        for i, h in enumerate(hits[:5]):
            (HERE / f".ok_resp_{i}.json").write_text(h["body"], encoding="utf-8")

        await page.screenshot(path=str(HERE / ".ok_search.png"))
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))