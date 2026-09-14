"""LLM 响应缓存（设计 6.4.4）。

``hash(model + prompt + temperature) → response``，落 DuckDB。回测重放、失败重试、
调试都直接命中，既省钱又保证可复现。P6 的可复现性很大程度靠它。
"""

from __future__ import annotations

import hashlib
import json
import logging
import duckdb
from ...storage.db import Database
import threading
import time
from pathlib import Path

from .base import LLMResponse

logger = logging.getLogger(__name__)


def _key(model: str, prompt: str, temperature: float) -> str:
    raw = f"{model}|{temperature:.3f}|{prompt}"
    return hashlib.sha256(raw.encode()).hexdigest()


class LLMCache:
    """DuckDB 响应缓存；与运行库共用进程所有者。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = Database(path, schema="cache")
        self._conn.execute("CREATE TABLE IF NOT EXISTS llm_cache (key VARCHAR PRIMARY KEY, model VARCHAR, response VARCHAR, created_at DOUBLE)")

    def get(self, model: str, prompt: str, temperature: float) -> LLMResponse | None:
        k = _key(model, prompt, temperature)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT response FROM llm_cache WHERE key=?", (k,)).fetchone()
        except duckdb.Error as exc:                 # 缓存故障绝不能冒充"LLM 故障"
            logger.warning("LLM 缓存读取失败（按未命中处理）: %s", exc)
            return None
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except Exception:  # pragma: no cover
            return None
        r = LLMResponse(**data)
        r.cached = True
        return r

    def put(self, model: str, prompt: str, temperature: float, resp: LLMResponse) -> None:
        k = _key(model, prompt, temperature)
        data = {
            "content": resp.content, "model": resp.model,
            "prompt_tokens": resp.prompt_tokens, "completion_tokens": resp.completion_tokens,
            "cost_cny": resp.cost_cny, "cached": False, "latency_ms": resp.latency_ms,
            "meta": resp.meta,
        }
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO llm_cache (key, model, response, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (k, model, json.dumps(data, ensure_ascii=False), time.time()))
        except duckdb.Error as exc:                 # 写缓存失败只是少省一次钱，不影响结果
            logger.warning("LLM 缓存写入失败（忽略）: %s", exc)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except duckdb.Error:                    # pragma: no cover
                pass
