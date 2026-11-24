# -*- coding: utf-8 -*-
"""从 1688 图搜结果页提取 offer id（结果卡片不是 <a href>，要换抓法）。"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
COOKIE_FILE = HERE / ".1688-cookie.txt"
REAL = re.compile(r"detail\.1688\.com/offer/(\d{9,16})\.html")
ANY_ID = re.compile(r"^(\d{9,16})$")


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
    img = HERE / ".img-cache" / "store-8c84ab1ffd749f700e53d39eec88fee7-small.jpg"
    if not img.exists():
        print("先跑 search_once.py 生成缩图")
        return 1

    captured: list[dict] = []
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
            if any(x in u for x in (".js", ".css", ".png", ".jpg", ".webp", ".woff")):
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct and "text/plain" not in ct:
                return
            try:
                body = await resp.text()
            except Exception:
                return
            if len(body) < 500:
                return
            # 搜索接口的特征：一批 offer 对象
            if body.count('"offerId"') >= 3 or body.count("offerId") >= 5:
                ids = re.findall(r'"offerId"\s*:\s*"?(\d{9,16})"?', body)
                if ids:
                    captured.append({"url": u[:200], "len": len(body),
                                     "ids": ids[:40], "body": body})
            elif "imageSearch" in body or "image_search" in body:
                captured.append({"url": u[:200], "len": len(body),
                                 "ids": [], "body": body})

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))

        # 走完整搜索
        await page.goto("https://air.1688.com/kapp/1688-search/pc-image-search/",
                        wait_until="domcontentloaded", timeout=120_000)
        await page.wait_for_timeout(4000)
        fi = await page.query_selector("input[type=file]")
        await fi.set_input_files(str(img))
        await page.wait_for_timeout(4000)
        try:
            await page.get_by_text("搜索图片", exact=True).click(timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(8000)

        print(f"[1] 捕获搜索接口 {len(captured)} 条")
        captured.sort(key=lambda x: -len(x["ids"]))
        for c in captured[:6]:
            print(f"    {c['len']:>8}B ids={len(c['ids']):>3} {c['url'][:100]}")
            print(f"        {c['ids'][:8]}")

        # 保存最大那条
        if captured:
            (HERE / ".result_api.json").write_text(
                captured[0]["body"], encoding="utf-8")
            print(f"    已存 .result_api.json ({captured[0]['len']} 字节)")

        # [2] DOM 兜底：找结果卡片的可点容器
        print("\n[2] DOM 提取")
        info = await page.evaluate("""() => {
          const out = {aHref: [], dataAttr: [], onclick: [], text: []};
          document.querySelectorAll('a[href]').forEach(a => {
            if (/offer/.test(a.href)) out.aHref.push(a.href);
          });
          document.querySelectorAll('*').forEach(el => {
            for (const at of el.attributes || []) {
              if (/^\\d{9,16}$/.test(at.value)) out.dataAttr.push(at.name + '=' + at.value);
              if (/offer/i.test(at.name) && /\\d{9,16}/.test(at.value))
                out.dataAttr.push(at.name + '=' + at.value.slice(0, 40));
            }
            if (el.onclick) out.onclick.push(String(el.onclick).slice(0, 100));
          });
          return out;
        }""")
        print(f"    a[href*offer]: {len(info['aHref'])}")
        for x in info["aHref"][:5]:
            print("      ", x)
        print(f"    data 属性带数字: {len(info['dataAttr'])}")
        for x in list(dict.fromkeys(info["dataAttr"]))[:10]:
            print("      ", x)
        print(f"    onclick: {len(info['onclick'])}")
        for x in list(dict.fromkeys(info["onclick"]))[:5]:
            print("      ", x)

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))