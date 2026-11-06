"""店铺仓库 ID 缓存：整批只查一次。

实测 warehouse 环节占认领总耗时 36.5%（每件都调 warehouse_list 约 10 秒）。
同一店铺的仓库 ID 在整批处理中不会变，缓存即可。

用法：
    from services.warehouse_cache import cached_warehouse_id
    wid = cached_warehouse_id(client, shop_id)
"""
from __future__ import annotations

import logging
import threading

from services.miaoshou import MiaoshouClient

logger = logging.getLogger("aohuang.warehouse")

_lock = threading.Lock()
_cache: dict[tuple[int, int], str] = {}


def cached_warehouse_id(client: MiaoshouClient, shop_id: int) -> str:
    """返回店铺默认仓库 ID（进程内缓存，避免每件都查一次）。"""
    key = (id(client), int(shop_id))
    with _lock:
        hit = _cache.get(key)
    if hit:
        return hit

    from services import carrier_service

    value = carrier_service.target_warehouse_id(client, int(shop_id))
    with _lock:
        _cache[key] = value
    logger.info("仓库 ID 已缓存 shop=%s warehouse=%s", shop_id, value)
    return value


def clear() -> None:
    with _lock:
        _cache.clear()
