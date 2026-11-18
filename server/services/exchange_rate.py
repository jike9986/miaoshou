"""多币种定价汇率：返回「每 1 单位目标币种」对应的 CNY 数值。

站点与币种的对应（与妙手官方运费表一致）：
    MY -> MYR    马来
    PH -> PHP    菲律宾
    TH -> THB    泰国
    VN -> VND    越南
    SG -> SGD    新加坡

定价时取汇率的优先顺序：
  1. 操作者手填的 override
  2. 有效缓存
  3. 公开汇率接口
  4. 内置常量，只在网络不可达时兜底

每条网络路径都是 best-effort 且超时很短：计算器离线也得能算，
不能因为查不到汇率就整个不可用。

历史包袱：这个模块原来只认 MYR（函数名、缓存结构、日志文案都写死）。
多站点支持后改成按币种分桶，但对外仍保留无参调用形式（等价于 MYR），
避免所有既有调用方一起改。
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

SITE_CURRENCY: dict[str, str] = {
    "MY": "MYR",
    "PH": "PHP",
    "TH": "THB",
    "VN": "VND",
    "SG": "SGD",
}

FALLBACK_RATES: dict[str, Decimal] = {
    "MYR": Decimal("1.6393"),
    "PHP": Decimal("0.1230"),
    "THB": Decimal("0.2160"),
    "VND": Decimal("0.00029"),
    "SGD": Decimal("5.3300"),
    "CNY": Decimal("1"),
}

DEFAULT_CURRENCY = "MYR"

FX_ENDPOINTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("open.er-api", "https://open.er-api.com/v6/latest/CNY", ("rates",)),
    ("exchangerate-api", "https://api.exchangerate-api.com/v4/latest/CNY", ("rates",)),
)
FX_TIMEOUT_SECONDS = 6
FX_CACHE_TTL_SECONDS = 3600
FX_FAILURE_CACHE_TTL_SECONDS = 30

_cache: dict[str, dict[str, Any]] = {}


def currency_for_site(site: Any) -> str:
    """站点码 -> 币种码。未知站点退回 MYR（历史默认）。"""
    key = str(site or "").strip().upper()
    return SITE_CURRENCY.get(key, DEFAULT_CURRENCY)


def _bucket(currency: str) -> dict[str, Any]:
    code = str(currency or DEFAULT_CURRENCY).strip().upper()
    return _cache.setdefault(
        code, {"rate": None, "at": 0.0, "source": "", "failedAt": None})


def _per_cny(payload: Any, keys: tuple[str, ...], currency: str) -> float | None:
    """从 JSON 里读出「1 CNY 等于多少目标币种」。"""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        block = payload.get(key)
        if isinstance(block, dict) and block.get(currency) not in (None, ""):
            try:
                return float(block[currency])
            except (TypeError, ValueError):
                continue
    return None


def live_rate(*, timeout: float = FX_TIMEOUT_SECONDS,
              use_cache: bool = True,
              currency: str = DEFAULT_CURRENCY) -> tuple[Decimal | None, str]:
    """查实时汇率；全失败短暂缓存后返回 (None, "")。

    currency 缺省 MYR，保持与旧调用方（无参）兼容。
    """
    code = str(currency or DEFAULT_CURRENCY).strip().upper()
    if code == "CNY":
        return Decimal("1"), "本币"

    bucket = _bucket(code)
    if use_cache:
        cached, source = cached_rate(currency=code)
        if cached is not None:
            return cached, source
        failed_at = bucket["failedAt"]
        if (failed_at is not None
                and 0 <= time.time() - failed_at < FX_FAILURE_CACHE_TTL_SECONDS):
            return None, ""

    for name, url, keys in FX_ENDPOINTS:
        try:
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
        per_cny = _per_cny(payload, keys, code)
        if not per_cny or per_cny <= 0:
            continue
        rate = Decimal(str(round(1.0 / per_cny, 8)))
        bucket.update(
            {"rate": rate, "at": time.time(), "source": name, "failedAt": None})
        logger.info("汇率已更新：1 %s = %s CNY（来源 %s）", code, rate, name)
        return rate, name
    bucket["failedAt"] = time.time()
    return None, ""


def cached_rate(*, currency: str = DEFAULT_CURRENCY) -> tuple[Decimal | None, str]:
    """返回有效期内的缓存汇率，不碰网络；过期返回 (None, "")。"""
    code = str(currency or DEFAULT_CURRENCY).strip().upper()
    if code == "CNY":
        return Decimal("1"), "本币"
    bucket = _bucket(code)
    if bucket["rate"] and 0 <= time.time() - bucket["at"] < FX_CACHE_TTL_SECONDS:
        return bucket["rate"], bucket["source"]
    return None, ""


def fallback_rate(currency: str = DEFAULT_CURRENCY) -> Decimal:
    """内置兜底汇率（网络完全不可用时用）。"""
    code = str(currency or DEFAULT_CURRENCY).strip().upper()
    return FALLBACK_RATES.get(code, FALLBACK_RATES[DEFAULT_CURRENCY])


def reset_cache() -> None:
    """清掉进程内缓存（测试与手工「重新取汇率」用）。"""
    _cache.clear()
