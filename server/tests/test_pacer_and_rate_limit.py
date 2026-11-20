"""节流（Pacer）与限流识别 —— 2026-11-20 实测修正。

## 负责人这一轮的追问

「故意等的 0.9 秒真的需要这么久吗」

## 实测三组（同载体、同 6 条链接、每轮间隔 4s）

    interval=0.90  总 6.16s   客户端睡 4.16s   上游 2.00s   6/6 成功
    interval=0.00  总 5.46s   客户端睡 0.00s   上游 5.46s   6/6 成功
    interval=0.35  总 6.07s   客户端睡 0.58s   上游 5.49s   6/6 成功

关掉节流只快 12%，因为上游自己慢了 2.7 倍 —— 妙手在服务端排队。
所以「故意白等」这个说法不成立：不睡客户端，就得在等响应时睡。

## 但确实找到两个真 bug

1. `UPSTREAM_PACE_MIN_SECONDS` 原来 = 0.90 = 默认值，导致
   `reward()` 的 `interval > MIN` 永远为假，**自适应降速从未生效**。
2. `_is_rate_limit` 不认识「请求太频繁」这句妙手真实返回的文案
   （压测时抓到的原文：`妙手接口失败 [fail]：请求太频繁，请稍后重试`），
   于是限流被当成业务错误 —— 既不退避也不抬间隔。
"""
from __future__ import annotations

import threading
import time


# --- 自适应必须真的能降（原 bug）------------------------------------------

def test_reward_can_actually_lower_interval():
    """`reward()` 必须能把 interval 降下来。

    原 bug：MIN == 默认值 0.90，`interval > MIN` 永远为假，
    于是惩罚抬到 1.5s 之后再也回不去，所有请求永久变慢。
    """
    from services.miaoshou import Pacer

    pacer = Pacer(interval=1.5)
    for _ in range(50 * 12):        # 每次降 0.05，需要若干轮
        pacer.reward()
    assert pacer.interval < 1.5, pacer.interval
    # 不能低于下限
    for _ in range(50 * 100):
        pacer.reward()
    assert pacer.interval >= 0.55, pacer.interval


def test_min_is_lower_than_default():
    """下限必须低于默认值，否则 reward 逻辑形同虚设。"""
    from core import constants

    assert constants.UPSTREAM_PACE_MIN_SECONDS < constants.UPSTREAM_PACE_SECONDS
    assert constants.UPSTREAM_PACE_MAX_SECONDS > constants.UPSTREAM_PACE_MIN_SECONDS


def test_penalize_raises_interval_up_to_max():
    from services.miaoshou import Pacer

    pacer = Pacer(interval=0.9)
    for _ in range(50):
        pacer.penalize()
    assert pacer.interval <= 1.5
    # 抬过之后仍然能降回去（原 bug 就卡在这里）
    for _ in range(50 * 20):
        pacer.reward()
    assert pacer.interval < 1.5


def test_wait_does_not_sleep_when_interval_zero():
    from services.miaoshou import Pacer

    pacer = Pacer(interval=0.0)
    started = time.monotonic()
    for _ in range(5):
        pacer.wait()
    assert time.monotonic() - started < 0.1


def test_wait_enforces_interval_between_calls():
    from services.miaoshou import Pacer

    pacer = Pacer(interval=0.2)
    pacer.wait()
    started = time.monotonic()
    pacer.wait()                      # 第二次必须等够 0.2s
    elapsed = time.monotonic() - started
    assert elapsed >= 0.18, elapsed


def test_pacer_is_thread_safe():
    """wait() 用了锁：并发调用不能互相穿插成「都没等」。"""
    from services.miaoshou import Pacer

    pacer = Pacer(interval=0.05)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker():
        for _ in range(4):
            pacer.wait()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    # 允许少量抖动，但不能出现大批「几乎同时」
    assert sum(1 for gap in gaps if gap < 0.01) <= 1, gaps


# --- 限流识别（原 bug：认不出妙手真实文案）---------------------------------

def test_recognizes_real_chinese_rate_limit_message():
    """压测抓到的原文必须被识别为限流。

    原文：`妙手接口失败 [fail]：请求太频繁，请稍后重试`
    旧关键词表里有「频率」「限流」「qps」，但**没有这句** ——
    于是它被归成业务错误，重试也不会好。
    """
    from services.miaoshou import _is_rate_limit

    assert _is_rate_limit("fail", "妙手接口失败 [fail]：请求太频繁，请稍后重试")
    assert _is_rate_limit("fail", "请求过于频繁")
    assert _is_rate_limit("fail", "操作太频繁，请稍后再试")


def test_recognizes_english_rate_limit_variants():
    from services.miaoshou import _is_rate_limit

    assert _is_rate_limit("accountApiQpsRateLimit", "")
    assert _is_rate_limit("fail", "QPS limit exceeded")
    assert _is_rate_limit("fail", "Rate limit reached")
    assert _is_rate_limit("fail", "Too Many Requests")
    assert _is_rate_limit("fail", "request throttled")
    assert _is_rate_limit("fail", "too frequent")


def test_does_not_misclassify_gateway_errors_as_rate_limit():
    """网关超时/繁忙不是限流 —— 混淆会让退避策略走错分支。"""
    from services.miaoshou import _is_rate_limit

    assert not _is_rate_limit("fail", "下游服务响应超时")
    assert not _is_rate_limit("fail", "下游服务繁忙")
    assert not _is_rate_limit("fail", "商品不存在")
    assert not _is_rate_limit("fail", "commonCollectBoxDetailId 不能为空")
    assert not _is_rate_limit("fail", "editCommonCollectBoxDetail.stock 不能大于99999")