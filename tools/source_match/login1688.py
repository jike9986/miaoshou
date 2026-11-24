# -*- coding: utf-8 -*-
"""登录 1688 并把会话存进专用 profile。

为什么必须登录（2026-11-24 实测）：
  · 未登录访问 s.1688.com/youyuan 会 302 到 login.taobao.com
  · 未登录请求图搜一律落 `punish` 滑块页 / `访问被拒绝`
  · 滑块是 baxia 系，脚本拖动会被升级成封禁，不是可靠路径

所以做法是：人工扫一次码，cookie 落盘，后续批量图搜全程复用。

用法：
    python tools/source_match/login1688.py
窗口打开后扫码，登录成功脚本会自动退出并打印会话状态。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE = Path(__file__).resolve().parent / ".profile-1688"
LOGIN_URL = "https://login.1688.com/member/signin.htm"


async def main() -> int:
    PROFILE.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)
        print("浏览器已打开。请用 1688 App 扫码登录，登录成功后会自动继续…", flush=True)

        # 轮询「是否已登录」：1688 登录成功后 login.taobao.com 会跳到 1688 首页，
        # 且 www.1688.com 下会种下 __cn_logon__ / cookie2。
        logged_in = False
        for i in range(200):  # 最多约 10 分钟
            await asyncio.sleep(3)
            try:
                cookies = await ctx.cookies("https://www.1688.com/")
                names = {c["name"] for c in cookies}
                if {"cookie2", "__cn_logon__"} & names or "__cn_logon__" in names:
                    logged_in = True
                    print(f"  检测到登录 cookie（{i * 3}s）", flush=True)
                    break
                url = page.url
                if "login" not in url and "1688.com" in url:
                    print(f"  已离开登录页：{url[:80]}（{i * 3}s）", flush=True)
                    logged_in = True
                    break
            except Exception:
                continue

        if not logged_in:
            print("未检测到登录成功，退出。", flush=True)
            await ctx.close()
            return 1

        # 验证：能不能进图搜页
        print("验证图搜页可访问性…", flush=True)
        await page.goto("https://s.1688.com/youyuan/index.htm?tab=imageSearch",
                        wait_until="domcontentloaded", timeout=90_000)
        await asyncio.sleep(3)
        url = page.url
        blocked = "punish" in url or "x5sec" in url or "login" in url
        print(f"  图搜页 URL: {url[:100]}", flush=True)
        print(f"  被拦: {blocked}", flush=True)
        await page.screenshot(path=str(PROFILE.parent / ".login_check.png"))

        cookies = await ctx.cookies("https://www.1688.com/")
        print(f"  cookie 数量: {len(cookies)}", flush=True)
        await ctx.close()
        return 0 if not blocked else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))