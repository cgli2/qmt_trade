"""Shared provider frame cache with explicit freshness checks."""
import time
from .db import Database
from .market import MarketRepository
from .runtime import runtime_path


class FrameCache:
    def __init__(self):
        self.repo = MarketRepository(Database(runtime_path(), schema="cache"))

    def fresh(self, namespace, key, ttl):
        meta = self.repo.metadata(namespace, key)
        return bool(ttl > 0 and meta and 0 <= time.time() - meta["written_at"] <= ttl)

    def get(self, namespace, key, ttl=86400):
        if not self.fresh(namespace, key, ttl):
            return None
        return self.repo.read(namespace, key)

    def put(self, namespace, key, frame):
        if frame is not None and not frame.empty:
            self.repo.write(namespace, frame, key)


def provider_cache(provider):
    cache = getattr(provider, "_duckdb_frames", None)
    if cache is None:
        cache = FrameCache()
        provider._duckdb_frames = cache
    return cache
