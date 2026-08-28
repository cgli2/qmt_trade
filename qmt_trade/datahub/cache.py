"""分层缓存。

修正 TradingAgents-CN 的缺陷 #12：它给 A 股行情设了 1 小时 TTL，
盘中拿到的是一小时前的价格 —— 对分析尚可，对交易是致命的。
本模块按数据类别分别设置 TTL，实时 Tick 明确**不缓存**。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable

from ..core.logging import get_logger

logger = get_logger("datahub.cache")

#: 各类数据的默认 TTL（秒）。0 表示不缓存，None 表示永久。
DEFAULT_TTL: dict[str, float | None] = {
    "tick": 0,            # ★ 实时行情绝不缓存
    "minute_bar": 60,
    "daily_bar": 86400,
    "fundamental": 86400,
    "instrument": 86400,
    # 新闻/事件是**不可变历史**（发布即定型），缓存 1 天：
    # 回测 49 天×4500 只逐票联网拉新闻原本要 ~98 小时，靠内存缓存跨日复用
    # 直接降到首轮一次 + 其余天秒级命中（性能修复 2026-08-12）。
    "news": 86400,
    "events": 86400,
    "factor": None,
    "index": 3600,
}


class TTLCache:
    """带 TTL 的线程安全 LRU 缓存。"""

    def __init__(self, max_items: int = 4096, default_ttl: float | None = 300):
        self.max_items = max_items
        self.default_ttl = default_ttl
        self._store: OrderedDict[Hashable, tuple[float | None, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def _expired(self, expire_at: float | None) -> bool:
        return expire_at is not None and time.time() >= expire_at

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                self.misses += 1
                return default
            expire_at, value = item
            if self._expired(expire_at):
                del self._store[key]
                self.misses += 1
                return default
            self._store.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: Hashable, value: Any, ttl: float | None = -1) -> None:
        effective = self.default_ttl if ttl == -1 else ttl
        if effective is not None and effective <= 0:
            return  # ttl=0 → 明确不缓存
        with self._lock:
            expire_at = None if effective is None else time.time() + effective
            self._store[key] = (expire_at, value)
            self._store.move_to_end(key)
            while len(self._store) > self.max_items:
                self._store.popitem(last=False)

    def get_or_set(self, key: Hashable, factory: Callable[[], Any], ttl: float | None = -1) -> Any:
        sentinel = object()
        val = self.get(key, sentinel)
        if val is not sentinel:
            return val
        val = factory()
        self.set(key, val, ttl)
        return val

    def invalidate(self, key: Hashable) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "size": len(self._store),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }


class CategoryCache:
    """按数据类别分桶的缓存，每类独立 TTL。"""

    def __init__(self, max_items: int = 4096, ttl_overrides: dict[str, float | None] | None = None):
        self.ttl = dict(DEFAULT_TTL)
        if ttl_overrides:
            self.ttl.update({k: v for k, v in ttl_overrides.items() if k in self.ttl or True})
        self._cache = TTLCache(max_items=max_items, default_ttl=300)

    def _key(self, category: str, key: Hashable) -> Hashable:
        return (category, key)

    def get(self, category: str, key: Hashable, default: Any = None) -> Any:
        if self.ttl.get(category) == 0:
            return default
        return self._cache.get(self._key(category, key), default)

    def set(self, category: str, key: Hashable, value: Any) -> None:
        ttl = self.ttl.get(category, 300)
        if ttl == 0:
            return
        self._cache.set(self._key(category, key), value, ttl)

    def get_or_set(self, category: str, key: Hashable, factory: Callable[[], Any]) -> Any:
        ttl = self.ttl.get(category, 300)
        if ttl == 0:
            return factory()
        return self._cache.get_or_set(self._key(category, key), factory, ttl)

    def clear(self) -> None:
        self._cache.clear()

    def stats(self) -> dict:
        return self._cache.stats()
