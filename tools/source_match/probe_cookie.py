# -*- coding: utf-8 -*-
"""用你 F12 里那串 cookie 直接打 1688 图搜：先验一张图能不能出结果。

用法：
    python tools/source_match/probe_cookie.py

cookie 从 `.1688-cookie.txt` 读（纯 cookie 串，或整条 `Cookie: ...` 都行）。
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
COOKIE_FILE = HERE / ".1688-cookie.txt"
IMG = ("https://aohuang888.com/tk-product-automation/products/"
       "store-8c84ab1ffd749f700e53d39eec88fee7.jpg")
OFFER_RE = re.compile(r"offer/(\d{11,14})")


def parse_cookie(raw: str) -> list[dict]:
    """把 `a=1; b=2` 解析成 Playwright 的 cookie 列表。"""
    raw = raw.strip()
    # 支持整条 "Cookie: xxx" 或 cURL -H 'cookie: xxx'
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    m = re.search(r"-H\s+['\"]cookie:\s*([^'\"]+)['\"]", raw, re.I)
    if m:
        raw = m.group(1)
    raw = raw.strip().strip("'\"")
    out = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        if not name:
            continue
        out.append({
            "name": name,
            "value": value.strip(),
            "domain": ".1688.com",
            "path": "/",
        })
    return out


async def main() -> int:
    if not COOKIE_FILE.exists():
        print(f"缺少 {COOKIE_FILE}")
        return 1
    cookies = parse_cookie(COOKIE_FILE.read_text(encoding="utf-8"))
    print(f"解析到 {len(cookies)} 个 cookie")
    names = {c["name"] for c in cookies}
    for want in ("cookie2", "unb", "__cn_logon__", "_tb_token_", "sg", "tfstk"):
        print(f"  {'✔' if want in names else '✘'} {want}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"])
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

        # 1) 先看登录态生效没
        print("\n[1] 验证登录态")
        await page.goto("https://www.1688.com/", wait_until="domcontentloaded",
                        timeout=90_000)
        await asyncio.sleep(3)
        print("    URL:", page.url[:90])
        html = await page.content()
        nick = re.search(r"tb139463474", html)
        print("    页面含会员名 tb139463474:", bool(nick))
        logged = await page.evaluate(
            "() => { const m=document.cookie.match(/__cn_logon__=([^;]+)/); "
            "return m ? m[1] : null; }")
        print("    __cn_logon__:", logged)

        # 2) 图搜（带 imageAddress）
        print("\n[2] 图搜（URL 传图）")
        url = ("https://s.1688.com/youyuan/index.htm?tab=imageSearch"
               f"&imageAddress={quote(IMG, safe='')}")
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        await asyncio.sleep(6)
        print("    URL:", page.url[:110])
        blocked = any(k in page.url for k in ("punish", "x5sec", "login", "denied"))
        print("    被拦:", blocked)
        html = await page.content()
        print("    HTML 长度:", len(html))
        ids = sorted({m.group(1) for m in OFFER_RE.finditer(html)})
        print("    HTML 里 offer:", len(ids), ids[:8])

        hrefs = await page.eval_on_selector_all("a[href]",
                                                "els => els.map(e => e.href)")
        hids = sorted({m.group(1) for h in hrefs
                       for m in [OFFER_RE.search(h)] if m})
        print("    链接里 offer:", len(hids), hids[:8])
        print("    链接总数:", len(hrefs))
        await page.screenshot(path=str(HERE / ".probe_cookie.png"),
                              full_page=False)

        # 3) 如果没有，试试上传本地文件
        if not ids and not hids and not blocked:
            print("\n[3] 上传本地文件方式")
            import requests
            local = HERE / ".probe.jpg"
            local.write_bytes(requests.get(IMG, timeout=30).content)
            inputs = await page.query_selector_all("input[type=file]")
            print("    file input:", len(inputs))
            if inputs:
                await inputs[0].set_input_files(str(local))
                await asyncio.sleep(8)
                html = await page.content()
                ids = sorted({m.group(1) for m in OFFER_RE.finditer(html)})
                hrefs = await page.eval_on_selector_all("a[href]",
                                                        "els => els.map(e => e.href)")
                hids = sorted({m.group(1) for h in hrefs
                               for m in [OFFER_RE.search(h)] if m})
                print("    上传后 HTML offer:", len(ids), ids[:8])
                print("    上传后链接 offer:", len(hids), hids[:8])
                await page.screenshot(path=str(HERE / ".probe_cookie_up.png"))

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))