"""LLM 客户端门面：把 adapter + cache + cost 串起来。

对外只暴露 ``complete()``：先查缓存 → 命中直接返回；未命中调 adapter →
记成本 → 写缓存。成本超预算时抛 ``LLMBudgetExceeded``，调用方据此降级纯因子模式（P5）。
"""

from __future__ import annotations

from .base import LLMAdapter, LLMResponse
from .cache import LLMCache
from .cost import CostTracker


class LLMClient:
    def __init__(self, adapter: LLMAdapter, *, cache: LLMCache | None = None,
                 tracker: CostTracker | None = None, cache_enabled: bool = True):
        self.adapter = adapter
        self.cache = cache or LLMCache()
        self.tracker = tracker or CostTracker()
        self.cache_enabled = cache_enabled

    def complete(self, prompt: str, *, model: str | None = None, scene: str | None = None,
                 temperature: float = 0.0, tag: str = "", use_cache: bool | None = None) -> LLMResponse:
        use_cache = self.cache_enabled if use_cache is None else use_cache
        if use_cache:
            hit = self.cache.get(model or self.adapter.name, prompt, temperature)
            if hit:
                self.tracker.record(hit, tag=tag)
                return hit
        resp = self.adapter.complete(prompt, model=model, temperature=temperature)
        self.tracker.record(resp, tag=tag)
        self.tracker.check()  # 超预算抛 LLMBudgetExceeded
        if use_cache:
            self.cache.put(model or self.adapter.name, prompt, temperature, resp)
        return resp

    def close(self) -> None:
        self.cache.close()
