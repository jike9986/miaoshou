"""CNY→MYR 实时汇率（老版 exchange_rate.py 的移植）。

取汇率的优先顺序，与老版一致：
  1. 操作者手填的 override
  2. 商品自身的 originPrice/price 比值——这是妙手实际用的汇率，
     真实跑批必须以它为准
  3. 公开汇率接口（给还没加载商品时的计算器用）
  4. 内置常量，只在网络不可达时兜底

每条网络路径都是 best-effort 且超时很短：计算器离线也得能算，
不能因为查不到汇率就整个不可用。
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

# 依次尝试。三个都在本机返回过合理汇率。
FX_ENDPOINTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("open.er-api", "https://open.er-api.com/v6/latest/CNY", ("rates",)),
    ("exchangerate-api", "https://api.exchangerate-api.com/v4/latest/CNY", ("rates",)),
    ("frankfurter", "https://api.frankfurter.app/latest?from=CNY&to=MYR", ("rates",)),
)
FX_TIMEOUT_SECONDS = 6
FX_CACHE_TTL_SECONDS = 3600

_cache: dict[str, Any] = {"rate": None, "at": 0.0, "source": ""}


def _myr_per_cny(payload: Any, keys: tuple[str, ...]) -> float | None:
    """从 JSON 里读出「1 CNY 等于多少 MYR」。"""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        block = payload.get(key)
        if isinstance(block, dict) and block.get("MYR") not in (None, ""):
            try:
                return float(block["MYR"])
            except (TypeError, ValueError):
                continue
    return None


def live_rate(*, timeout: float = FX_TIMEOUT_SECONDS,
              use_cache: bool = True) -> tuple[Decimal | None, str]:
    """查实时汇率，返回 (汇率, 来源)；全失败返回 (None, "")。"""
    now = time.time()
    if use_cache and _cache["rate"] and now - _cache["at"] < FX_CACHE_TTL_SECONDS:
        return _cache["rate"], _cache["source"]

    for name, url, keys in FX_ENDPOINTS:
        try:
            # 用 httpx 而不是 urllib：工程里已经依赖它，且不用管系统代理。
            import httpx

            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                response = client.get(
                    url, headers={"User-Agent": "aohuang-desktop/1.0"})
                if response.status_code >= 400:
                    continue
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 - 换下一个接口
            logger.debug("汇率接口 %s 不可用：%s", name, exc)
            continue
        per_cny = _myr_per_cny(payload, keys)
        if not per_cny or per_cny <= 0:
            continue
        rate = Decimal(str(round(1.0 / per_cny, 6)))
        _cache.update({"rate": rate, "at": now, "source": name})
        logger.info("汇率已更新：1 CNY = %s MYR（来源 %s）", rate, name)
        return rate, name
    return None, ""


def cached_rate() -> tuple[Decimal | None, str]:
    """上次成功取到的汇率，不碰网络。"""
    return _cache["rate"], _cache["source"]


def reset_cache() -> None:
    """清掉进程内缓存（测试与手工「重新取汇率」用）。"""
    _cache.update({"rate": None, "at": 0.0, "source": ""})
