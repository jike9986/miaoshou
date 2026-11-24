# -*- coding: utf-8 -*-
"""真上传图片触发 1688 图搜，抓所有带 offer 的响应。

前面用 `imageAddress=` 传图没触发搜索（只有热词/推荐接口），
说明新版 air.1688.com 页面必须真实走一次文件上传。
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
    if not local.exists() or local.stat().st_size < 1024:
        local.write_bytes(requests.get(IMG, timeout=45).content)
    print(f"本地图: {local} ({local.stat().st_size} 字节)")

    json_hits: list[dict] = []

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
            if "alicdn" in u and ".js" in u:
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if not any(k in ct for k in ("json", "text/plain", "javascript")):
                return
            try:
                body = await resp.text()
            except Exception:
                return
            if len(body) < 200 or "offer" not in body.lower():
                return
            ids = sorted({m.group(1) for m in OFFER.finditer(body)})
            if not ids:
                return
            json_hits.append({"url": u[:300], "len": len(body),
                              "ids": ids[:30], "body": body})

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))

        print("\n[1] 打开图搜首页")
        await page.goto("https://air.1688.com/kapp/1688-search/pc-image-search/",
                        wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(5)
        print("    URL:", page.url[:110])

        print("\n[2] 找上传入口")
        sels = ["input[type=file]", "[class*=upload] input", "[class*=camera]",
                "[class*=image-search]", "[class*=pic-search]"]
        target = None
        for sel in sels:
            els = await page.query_selector_all(sel)
            print(f"    {sel}: {len(els)}")
            if els and target is None and "input" in sel:
                target = els[0]

        if target is None:
            # 有些实现把 input 藏在页面上，兜底全量扫
            all_inputs = await page.query_selector_all("input")
            print(f"    全部 input: {len(all_inputs)}")
            for i, el in enumerate(all_inputs):
                t = await el.get_attribute("type")
                print(f"      [{i}] type={t}")
                if t == "file":
                    target = el
                    break

        if target is None:
            print("    ✘ 找不到 file input，存页面分析")
            html = await page.content()
            (HERE / ".air_noupload.html").write_text(html, encoding="utf-8")
            await page.screenshot(path=str(HERE / ".air_nofile.png"))
            await browser.close()
            return 2

        print("\n[3] 上传图片")
        await target.set_input_files(str(local))
        await asyncio.sleep(3)
        print("    上传后 URL:", page.url[:130])

        # 等结果
        for i in range(20):
            await asyncio.sleep(2)
            cards = await page.eval_on_selector_all(
                "a[href*='offer']", "els => els.map(e => e.href)")
            if cards:
                print(f"    第 {i*2}s 出现 {len(cards)} 个 offer 链接")
                break
        for _ in range(4):
            await page.mouse.wheel(0, 1200)
            await asyncio.sleep(1.5)

        cards = await page.eval_on_selector_all(
            "a[href*='offer']", "els => els.map(e => e.href)")
        dom_ids = sorted({m.group(1) for h in cards
                          for m in [OFFER.search(h)] if m})
        print(f"    DOM offer: {len(dom_ids)}")
        for x in dom_ids[:10]:
            print("      ", x)

        print(f"\n[4] 捕获 {len(json_hits)} 条含 offer 的响应")
        json_hits.sort(key=lambda x: -len(x["ids"]))
        for h in json_hits[:10]:
            print(f"    {h['len']:>8}B ids={len(h['ids']):>3}  {h['url'][:100]}")

        for i, h in enumerate(json_hits[:6]):
            (HERE / f".up_resp_{i}.json").write_text(h["body"], encoding="utf-8")

        await page.screenshot(path=str(HERE / ".air_uploaded.png"))
        await browser.close()

    print("\n完成。截图 .air_uploaded.png")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))