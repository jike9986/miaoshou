# -*- coding: utf-8 -*-
"""1688 图搜完整流程（已打通上传环节）。

关键结论（2026-11-24 实测）：
  · 原图 800x800 / 200KB → 上传后计数停在 (0/6)，缩略图灰块
  · 缩到 600x600 / 84KB → 页面出现「已上传1张图片」+ 预览图
  · 之后必须点「搜索图片」，不点就一直「空空如也」

所以批量流程 = 缩图 → set_input_files → 点「搜索图片」→ 抓结果 → 取第一条 offer。
"""
from __future__ import annotations

import asyncio
import io
import re
import sys
from pathlib import Path

import requests
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
COOKIE_FILE = HERE / ".1688-cookie.txt"
CACHE = HERE / ".img-cache"
REAL = re.compile(r"detail\.1688\.com/offer/(\d{9,16})\.html")
COUNT = re.compile(r"上传图片\s*\(\s*(\d+)\s*/\s*(\d+)\s*\)")


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


def prepare_image(url: str) -> Path:
    """下载并缩图到 600px（超过这个尺寸 1688 不认）。"""
    from PIL import Image

    CACHE.mkdir(parents=True, exist_ok=True)
    name = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "img.jpg"
    dest = CACHE / (name.rsplit(".", 1)[0] + "-small.jpg")
    if dest.exists() and dest.stat().st_size > 2048:
        return dest
    resp = requests.get(url, timeout=45)
    resp.raise_for_status()
    im = Image.open(io.BytesIO(resp.content)).convert("RGB")
    im.thumbnail((600, 600))
    im.save(dest, "JPEG", quality=90)
    return dest


async def one_search(page, img: Path, *, collect: int = 10) -> list[str]:
    """上传 + 搜索，返回结果里的 offer id（按页面顺序）。"""
    await page.goto("https://air.1688.com/kapp/1688-search/pc-image-search/",
                    wait_until="domcontentloaded", timeout=120_000)
    if "login" in page.url:
        raise RuntimeError("1688 会话已失效，重新取 cookie")
    await page.wait_for_timeout(4000)

    fi = await page.query_selector("input[type=file]")
    if not fi:
        raise RuntimeError("找不到上传入口（1688 可能改版）")
    await fi.set_input_files(str(img))

    # 等「已上传N张图片」出现
    uploaded = False
    for _ in range(12):
        await page.wait_for_timeout(1500)
        body = await page.inner_text("body")
        if "已上传" in body or "搜索图片" in body:
            uploaded = True
            break
    if not uploaded:
        raise RuntimeError("上传未生效")

    # 点「搜索图片」（不是顶部那个「搜索」）
    clicked = False
    for sel in ("text=搜索图片", "[class*=search-image]", "button:has-text('搜索图片')"):
        try:
            els = await page.query_selector_all(sel)
            for el in els:
                if await el.is_visible():
                    await el.click()
                    clicked = True
                    break
            if clicked:
                break
        except Exception:
            continue
    if not clicked:
        # 兜底：找包含「搜索图片」的最内层可点元素
        try:
            await page.get_by_text("搜索图片", exact=True).click(timeout=5000)
            clicked = True
        except Exception:
            pass
    if not clicked:
        raise RuntimeError("找不到「搜索图片」按钮")

    # 等结果
    ids: list[str] = []
    for _ in range(20):
        await page.wait_for_timeout(2000)
        hrefs = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)")
        ids = sorted({m.group(1) for h in hrefs
                      for m in [REAL.search(h)] if m})
        if len(ids) >= 1:
            break
        await page.mouse.wheel(0, 800)
    if len(ids) < collect:
        html = await page.content()
        more = {m.group(1) for m in REAL.finditer(html)}
        ids = sorted(set(ids) | more)
    return ids


async def main() -> int:
    cookies = parse_cookie(COOKIE_FILE.read_text(encoding="utf-8"))
    IMG = ("https://aohuang888.com/tk-product-automation/products/"
           "store-8c84ab1ffd749f700e53d39eec88fee7.jpg")
    img = prepare_image(IMG)
    print(f"缩好的图: {img.name} ({img.stat().st_size} 字节)")

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

        try:
            ids = await one_search(page, img)
            print(f"\n拿到 offer: {len(ids)}")
            for x in ids[:10]:
                print("  ", x)
            await page.screenshot(path=str(HERE / ".final_ok.png"))
        except Exception as exc:
            print(f"失败 {type(exc).__name__}: {exc}")
            await page.screenshot(path=str(HERE / ".final_fail.png"))
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))