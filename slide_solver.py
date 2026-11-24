# -*- coding: utf-8 -*-
"""阿里 baxia 滑块自动拖动（仿人轨迹 + 随机抖动）。

结构（实测）:
  #scratch-captcha-btn-icon  —— 16x16 的滑块手柄
  .scratch-captcha-slider    —— 320px 宽滑轨
"""
import asyncio, random, math

async def solve_slider(page, *, timeout_ms=15000):
    """尝试过滑块。返回 True 表示看起来过了（punish 页消失）。"""
    try:
        btn = await page.wait_for_selector("#scratch-captcha-btn-icon",
                                           timeout=timeout_ms)
    except Exception:
        return False
    box = await btn.bounding_box()
    if not box:
        return False
    track = await page.query_selector(".scratch-captcha-slider")
    tbox = await track.bounding_box() if track else None
    if not tbox:
        return False

    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    # 目标：滑轨右端（留 4px 余量）
    distance = (tbox["x"] + tbox["width"] - 4) - start_x

    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.06, 0.14))

    # 仿人：先快后慢（ease-out）+ 轻微上下抖动 + 偶尔回退
    steps = random.randint(28, 44)
    moved = 0.0
    for i in range(1, steps + 1):
        p = i / steps
        # easeOutQuad
        eased = 1 - (1 - p) ** 2
        target = distance * eased
        step = target - moved
        moved = target
        # 抖动
        jitter_y = math.sin(p * math.pi * 3) * random.uniform(0.4, 1.6)
        await page.mouse.move(start_x + moved,
                              start_y + jitter_y,
                              steps=1)
        await asyncio.sleep(random.uniform(0.012, 0.035))
        # 中段偶尔小幅回退（更像人）
        if 0.35 < p < 0.5 and random.random() < 0.35:
            await page.mouse.move(start_x + moved - random.uniform(1.5, 3.5),
                                  start_y, steps=1)
            await asyncio.sleep(random.uniform(0.03, 0.07))

    await asyncio.sleep(random.uniform(0.1, 0.25))
    await page.mouse.up()
    await asyncio.sleep(2.5)
    return True
