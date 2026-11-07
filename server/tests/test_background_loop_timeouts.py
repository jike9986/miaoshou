
"""回归：后台循环必须能用 asyncio.TimeoutError 接住等待超时。

服务器跑的是 Python 3.10，那里 asyncio.TimeoutError 与内置 TimeoutError
是两个类。main.py 的 _schedule_loop / _image_reconcile_loop 写过
`except TimeoutError: continue`，在 3.10 上一个都接不住 —— 循环等第一次
超时就抛出去、协程静默结束，线上表现是「后台跑一阵就不动了」「定时任务
设置好了不执行」（2026-11-07 实证：20:51:10 启动、20:51:30 图片循环
exception=TimeoutError() 死亡）。本地 3.11 两者同名，测不出这个坑，
所以这里直接读源码断言写法。
"""
from __future__ import annotations

import re
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "main.py"


def test_background_loops_catch_asyncio_timeout_error():
    source = MAIN.read_text(encoding="utf-8")
    # 裸 except TimeoutError 在 3.10 上抓不住 asyncio 的超时
    assert not re.search(r"except\s+TimeoutError\s*:", source), (
        "必须写 except asyncio.TimeoutError：Python 3.10 里它和内置 TimeoutError "
        "不是同一个类，写成后者会让后台循环在第一次等待超时后静默退出"
    )
    assert source.count("except asyncio.TimeoutError:") >= 2, (
        "图片回收循环与定时扫描循环都要能接住 asyncio.TimeoutError"
    )
