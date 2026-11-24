# -*- coding: utf-8 -*-
"""打开 1688 登录窗口，等人工「拖滑块 + 扫码」完成后自动检测并保存会话。

和 login1688.py 的区别：这个不主动跳图搜页（避免一进去就触发风控），
只在 www.1688.com 上轮询登录态。人工操作不做任何自动拖动。

用法：
    python tools/source_match/login_manual.py
窗口里做两件事：1) 拖动滑块过验证  2) 扫码登录
检测到 __cn_logon__ 后自动关闭并打印结果。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".profile-1688"
SHOT = HERE / ".login_final.png"


async def main() -> int:
    PROFILE.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
            viewport=None,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        await page.goto("https://www.1688.com/", wait_until="domcontentloaded",
                        timeout=90_000)
        print("=" * 66, flush=True)
        print("窗口已打开。请依次完成：", flush=True)
        print("  1) 如果看到滑块，手动拖过去", flush=True)
        print("  2) 点右上角「登录」→ 用 1688 App 扫码", flush=True)
        print("脚本会自己检测登录成功，不需要回来按键。", flush=True)
        print("=" * 66, flush=True)

        last_note = ""
        for i in range(400):  # 最长约 20 分钟
            await asyncio.sleep(3)
            try:
                cookies = await ctx.cookies("https://www.1688.com/")
            except Exception:
                continue
            names = {c["name"] for c in cookies}
            # 1688 登录后一定有 __cn_logon__（值含 id/ nick 等）
            if "__cn_logon__" in names or "cookie17" in names:
                print(f"  登录成功（{i * 3}s），cookie {len(cookies)} 个", flush=True)
                await page.screenshot(path=str(SHOT))
                print(f"  截图：{SHOT}", flush=True)
                # 顺手确认图搜页能否进（此时已登录）
                print("  验证图搜页…", flush=True)
                try:
                    await page.goto(
                        "https://s.1688.com/youyuan/index.htm?tab=imageSearch",
                        wait_until="domcontentloaded", timeout=90_000)
                    await asyncio.sleep(4)
                    cur = page.url
                    blocked = any(k in cur for k in ("punish", "x5sec", "login"))
                    print(f"    url={cur[:100]}", flush=True)
                    print(f"    被拦={blocked}", flush=True)
                    inputs = await page.query_selector_all("input[type=file]")
                    print(f"    图搜上传入口={len(inputs)} 个", flush=True)
                    await page.screenshot(path=str(HERE / ".login_search.png"))
                except Exception as exc:  # noqa: BLE001
                    print(f"    验证异常 {type(exc).__name__}: {str(exc)[:80]}",
                          flush=True)
                await ctx.close()
                return 0

            note = f"等待中… {i * 3}s"
            if note[: -len(str(i * 3))] != last_note:
                last_note = note[: -len(str(i * 3))]
            if i % 10 == 0:
                print(f"  {note}（进度只打印每 30 秒一次）", flush=True)

        print("超时未检测到登录。", flush=True)
        await ctx.close()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))