# -*- coding: utf-8 -*-
"""验证 1688 图搜的上传环节到底怎么才成功。

诊断点：
  · set_input_files 之后 (0/6) 没变 → 上传没触发
  · 缩略图是灰块 → 浏览器解码失败
所以这里换三种图源：本地生成的小 PNG / 已下载的 jpg / 直接拖拽上传。
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
IMG = ("https://aohuang888.com/tk-product-automation/products/"
       "store-8c84ab1ffd749f700e53d39eec88fee7.jpg")


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


def make_small_png() -> Path:
    """生成 200x200 纯色 PNG（保证能解码）。"""
    try:
        from PIL import Image
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "pillow",
                        "--quiet"], check=False)
        from PIL import Image
    p = HERE / ".small.png"
    Image.new("RGB", (200, 200), (40, 40, 40)).save(p, "PNG")
    return p


def resize_jpg(src: Path) -> Path:
    """把商品图缩到 500px 并重存为 jpg（1688 对超大图可能拒）。"""
    try:
        from PIL import Image
    except ImportError:
        return src
    p = HERE / ".resized.jpg"
    im = Image.open(src).convert("RGB")
    im.thumbnail((600, 600))
    im.save(p, "JPEG", quality=90)
    print(f"    缩图: {im.size} -> {p.stat().st_size} 字节")
    return p


async def main() -> int:
    cookies = parse_cookie(COOKIE_FILE.read_text(encoding="utf-8"))
    src = HERE / ".upload.jpg"
    if not src.exists():
        src.write_bytes(requests.get(IMG, timeout=45).content)
    from PIL import Image
    print(f"原图: {Image.open(src).size}, {src.stat().st_size} 字节")
    small = resize_jpg(src)
    tiny = make_small_png()

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

        # 监视上传请求
        uploads = []

        async def on_req(req):
            u = req.url
            if any(k in u.lower() for k in ("upload", "image", "pic", "sts")):
                if req.method == "POST" or "upload" in u.lower():
                    uploads.append({"url": u[:180], "method": req.method})

        page.on("request", lambda r: asyncio.create_task(on_req(r)))

        print("\n[1] 打开图搜，试 set_input_files（缩小图）")
        await page.goto("https://air.1688.com/kapp/1688-search/pc-image-search/",
                        wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(5)
        fi = await page.query_selector("input[type=file]")
        print(f"    input accept={await fi.get_attribute('accept')}")
        await fi.set_input_files(str(small))
        for i in range(10):
            await asyncio.sleep(2)
            txt = await page.inner_text("body")
            m = re.search(r"上传图片\s*\((\d+)/(\d+)\)", txt)
            print(f"    {i*2}s: {m.group(0) if m else '?'}")
            imgs = await page.eval_on_selector_all(
                "img[src]", "els=>els.map(e=>e.src).filter(s=>s.startsWith('data:')||s.includes('blob'))")
            if imgs:
                print(f"    出现预览图: {len(imgs)}")
                break
            if m and int(m.group(1)) > 0:
                break

        print(f"\n    上传相关请求: {len(uploads)}")
        for u in uploads[:10]:
            print(f"      {u['method']} {u['url']}")

        # 看页面状态
        txt = await page.inner_text("body")
        print(f"\n    页面文字片段: {txt[:200]!r}")
        await page.screenshot(path=str(HERE / ".upload_diag.png"))

        # 若还是 0，试试 drag-drop
        m = re.search(r"上传图片\s*\((\d+)/(\d+)\)", txt)
        if not m or int(m.group(1)) == 0:
            print("\n[2] set_input_files 无效，改用 drop 事件")
            data = small.read_bytes()
            b64 = __import__("base64").b64encode(data).decode()
            js = """
            async ([b64, name]) => {
              const bin = atob(b64);
              const arr = new Uint8Array(bin.length);
              for (let i=0;i<bin.length;i++) arr[i]=bin.charCodeAt(i);
              const file = new File([arr], name, {type:'image/jpeg'});
              const dt = new DataTransfer();
              dt.items.add(file);
              const zone = document.querySelector('input[type=file]')?.parentElement
                        || document.body;
              for (const type of ['dragenter','dragover','drop']) {
                const ev = new DragEvent(type, {bubbles:true, cancelable:true,
                                                dataTransfer:dt});
                zone.dispatchEvent(ev);
              }
              return true;
            }
            """
            try:
                await page.evaluate(js, [b64, "probe.jpg"])
                await asyncio.sleep(6)
                txt2 = await page.inner_text("body")
                m2 = re.search(r"上传图片\s*\((\d+)/(\d+)\)", txt2)
                print(f"    drop 后: {m2.group(0) if m2 else '?'}")
            except Exception as exc:
                print(f"    drop 失败 {type(exc).__name__}: {str(exc)[:80]}")

        await page.screenshot(path=str(HERE / ".upload_diag2.png"))
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))