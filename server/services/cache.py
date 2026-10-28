"""轻量 TTL 缓存（进程内、线程安全）。

为什么需要它：``collect_box_service.list_items`` 每次请求都要实时打妙手开放
接口，为了凑够一页还会 while 翻页最多 10 次；上游不可达时用户点开「采集箱」
就得干等整段超时。老版有本地缓存，新版没有——这里把它补回来。

设计约束：

* **只存成功结果**。异常永不进表，否则一次上游抽风会被缓存放大成持续故障；
* **线程安全**。FastAPI 的同步路由跑在线程池里，同一个缓存会被多个线程并发
  读写，所有字典操作都过一把 ``threading.Lock``；
* **写读隔离**。存/取都做一次 ``copy.deepcopy``，调用方改返回值（比如给每个
  item 补仓库标注）不会污染缓存里的原件；
* **stale_while_reload**。过期条目留在表里不立刻扔：上游这次失败时可以拿旧值
  兜底（见 :meth:`TTLCache.fetch` 的 ``degraded``），但过期多久必须如实报出来
  （``stale_seconds``），不能假装还是新数据。

用法::

    from services.cache import get_cache, ttl_from_env

    store = get_cache("collect_box", max_entries=256)
    result = store.fetch(key, load, ttl=ttl_from_env("AOHUANG_COLLECT_BOX_CACHE_TTL", 60.0),
                         stale_on=(MiaoshouError,))
"""
from __future__ import annotations

import copy
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CacheEntry",
    "FetchResult",
    "TTLCache",
    "clear_all",
    "get_cache",
    "get_cache_names",
    "ttl_from_env",
]


def ttl_from_env(name: str, default: float) -> float:
    """用环境变量覆盖 TTL；空值、非数字、非正数一律回退到 ``default``。

    每次调用都重新读环境变量（不在 import 时固化），测试和线上改配置都不必
    重启进程重新导入模块。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else float(default)


@dataclass(frozen=True)
class CacheEntry:
    """一条缓存记录。``stored_mono`` 判过期（不受系统改时间影响），
    ``stored_at`` 用于给用户算「旧了多少秒」。"""

    key: str
    value: Any
    stored_at: float
    stored_mono: float
    ttl: float

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.stored_at)

    def is_fresh(self, now_mono: float | None = None) -> bool:
        current = time.monotonic() if now_mono is None else now_mono
        return (current - self.stored_mono) < self.ttl


@dataclass(frozen=True)
class FetchResult:
    """:meth:`TTLCache.fetch` 的结果。

    * ``cached``：这次是否吃了缓存（新鲜命中或旧值兜底）；
    * ``degraded``：上游这次失败了，返回的是旧值；
    * ``stale_seconds``：旧值的年龄（秒），``degraded=False`` 时恒为 0。
    """

    value: Any
    cached: bool = False
    degraded: bool = False
    stale_seconds: int = 0


class TTLCache:
    """固定容量 + TTL 的进程内缓存。"""

    def __init__(self, name: str = "default", *, max_entries: int = 256,
                 default_ttl: float = 60.0) -> None:
        self.name = name
        self.max_entries = max(1, int(max_entries))
        self.default_ttl = float(default_ttl)
        self._lock = threading.Lock()
        self._data: dict[str, CacheEntry] = {}
        # 简单的可观测计数，排查「缓存到底有没有生效」时直接读
        self.hits = 0
        self.misses = 0
        self.stale_hits = 0

    # --- 读 ---------------------------------------------------------------
    def _peek(self, key: str, *, count: bool) -> CacheEntry | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                if count:
                    self.misses += 1
                return None
            if count:
                if entry.is_fresh():
                    self.hits += 1
                else:
                    self.stale_hits += 1
            return CacheEntry(key=entry.key, value=copy.deepcopy(entry.value),
                              stored_at=entry.stored_at, stored_mono=entry.stored_mono,
                              ttl=entry.ttl)

    def lookup(self, key: str) -> CacheEntry | None:
        """取条目，**包含已过期**的（供 stale_while_reload 用）。

        返回的是深拷贝，调用方随便改。
        """
        return self._peek(key, count=True)

    def get(self, key: str, default: Any = None) -> Any:
        """只返回**未过期**的值；不存在或已过期返回 ``default``。"""
        entry = self.lookup(key)
        if entry is None or not entry.is_fresh():
            return default
        return entry.value

    def has(self, key: str) -> bool:
        """是否有未过期的值。"""
        entry = self._peek(key, count=False)
        return entry is not None and entry.is_fresh()

    def age(self, key: str) -> float | None:
        """该 key 现存条目（哪怕过期）已经存在多少秒；没有则 None。"""
        entry = self._peek(key, count=False)
        return None if entry is None else entry.age_seconds

    # --- 写 ---------------------------------------------------------------
    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """写入一条成功结果。``ttl <= 0`` 视为关闭缓存，顺手删掉旧值兜底，
        避免下次上游失败时捡到一条本来就该失效的数据。"""
        resolved = float(self.default_ttl if ttl is None else ttl)
        if resolved <= 0:
            self.delete(key)
            return
        snapshot = copy.deepcopy(value)
        with self._lock:
            self._evict_locked()
            self._data[key] = CacheEntry(key=key, value=snapshot, stored_at=time.time(),
                                         stored_mono=time.monotonic(), ttl=resolved)

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None

    def clear(self) -> int:
        """清空，返回删掉的条数。"""
        with self._lock:
            count = len(self._data)
            self._data.clear()
            return count

    def invalidate(self, prefix: str = "") -> int:
        """按前缀失效（写操作之后用），返回删掉的条数。

        ``prefix=""`` 等价于 :meth:`clear`。
        """
        with self._lock:
            if not prefix:
                count = len(self._data)
                self._data.clear()
                return count
            victims = [key for key in self._data if key.startswith(prefix)]
            for key in victims:
                self._data.pop(key, None)
            return len(victims)

    # --- 组合：读缓存 → 回源 → 写缓存 -----------------------------------
    def fetch(self, key: str, load: Callable[[], Any], *, ttl: float | None = None,
              stale_on: tuple[type[BaseException], ...] | type[BaseException] = (Exception,),
              ) -> FetchResult:
        """读缓存；没有再调 ``load()``；成功才写缓存。

        线上游失败（异常类型命中 ``stale_on``）且表里有旧值（哪怕已过期）时，
        返回旧值并标 ``degraded=True``；没有旧值就原样抛出，由调用方走原来的
        错误映射。
        """
        entry = self.lookup(key)
        if entry is not None and entry.is_fresh():
            return FetchResult(value=entry.value, cached=True)
        try:
            value = load()
        except stale_on:
            if entry is None:
                raise
            return FetchResult(value=entry.value, cached=True, degraded=True,
                               stale_seconds=int(entry.age_seconds))
        self.set(key, value, ttl)
        return FetchResult(value=copy.deepcopy(value))

    # --- 内部 -------------------------------------------------------------
    def _evict_locked(self) -> None:
        """超容量时先扔过期条目，再按最久未更新淘汰。"""
        if len(self._data) < self.max_entries:
            return
        now = time.monotonic()
        expired = [key for key, entry in self._data.items() if not entry.is_fresh(now)]
        for key in expired:
            self._data.pop(key, None)
        while len(self._data) >= self.max_entries:
            oldest = min(self._data, key=lambda key: self._data[key].stored_mono)
            self._data.pop(oldest, None)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "entries": len(self._data),
                "maxEntries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "staleHits": self.stale_hits,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# --- 命名缓存的全局注册表 ---------------------------------------------------
_CACHES: dict[str, TTLCache] = {}
_REGISTRY_LOCK = threading.Lock()


def get_cache(name: str, *, max_entries: int = 256,
              default_ttl: float = 60.0) -> TTLCache:
    """按名字取单例缓存（首次调用时创建）。"""
    with _REGISTRY_LOCK:
        store = _CACHES.get(name)
        if store is None:
            store = TTLCache(name, max_entries=max_entries, default_ttl=default_ttl)
            _CACHES[name] = store
        return store


def get_cache_names() -> list[str]:
    with _REGISTRY_LOCK:
        return sorted(_CACHES)


def clear_all() -> int:
    """清空所有命名缓存（测试、账号切换时用），返回总条数。"""
    with _REGISTRY_LOCK:
        stores = list(_CACHES.values())
    return sum(store.clear() for store in stores)
