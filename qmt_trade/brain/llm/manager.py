"""LLM 统一管理层（多 provider / 多 model / 场景智能选模 / 成本熔断 / 缓存）。

对外主入口 ``complete(prompt, *, scene=, model=, temperature=, tag=)``：

- ``model`` 显式指定：该模型优先，其余按场景排名为 fallback；
- ``scene`` 指定：按 ``ModelSelector`` 综合分排序候选链；
- 都没给：用 ``default_model``。

链上每个模型失败（网络/结构异常）自动尝试下一个；全部失败抛 ``LLMCallFailed``，
由 Agent 层捕获降级规则路径（P5）。预算超限抛 ``LLMBudgetExceeded``（P4 降级纯因子）。
"""

from __future__ import annotations

import logging
import threading

from .registry import LLMConfig, load_llm_config
from .health import ModelHealth
from .selector import ModelSelector
from .cache import LLMCache, _key
from .cost import CostTracker
from .base import LLMResponse
from ...core.errors import LLMCallFailed, LLMBudgetExceeded

logger = logging.getLogger(__name__)


class LLMManager:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.enabled = bool(cfg.enabled)
        self.health: dict[str, ModelHealth] = {m.id: ModelHealth(m.id) for m in cfg.models}
        self.selector = ModelSelector(cfg, self.health)
        self.cost = CostTracker(
            daily_budget_cny=float(cfg.budget.get("daily_cny", 30.0)),
            monthly_budget_cny=float(cfg.budget.get("monthly_cny", 600.0)),
        )
        self.cache = LLMCache(cfg.cache_path) if cfg.cache_enabled else None
        #: 审计钩子：每次**真实调用**成功后回调（缓存命中不回调，避免重复记账）。
        #: 由应用层挂接落库 llm_calls 表；钩子异常不影响主流程。
        self.audit = None
        self._adapters: dict[str, object] = {}
        self._lock = threading.Lock()

    # ---- 装配 ----
    @classmethod
    def from_file(cls, path=None) -> "LLMManager":
        return cls(load_llm_config(path))

    def _adapter_for(self, model_id: str):
        with self._lock:
            ad = self._adapters.get(model_id)
            if ad is not None:
                return ad
        from .factory import build_adapter
        model = self.cfg.get_model(model_id)
        prov = self.cfg.get_provider(model.provider) if model else None
        ad = build_adapter(prov, model)
        with self._lock:
            self._adapters[model_id] = ad
        return ad

    # ---- 主入口 ----
    def complete(self, prompt: str, *, scene: str | None = None, model: str | None = None,
                 temperature: float | None = None, tag: str = "") -> LLMResponse:
        if not self.enabled:
            raise LLMCallFailed("LLM 未启用（纯因子模式）")
        try:
            self.cost.check()                      # P5：超预算直接降级，而非仅告警
        except LLMBudgetExceeded:
            raise

        temp = self.cfg.temperature if temperature is None else temperature
        if model:
            # 显式指定模型：该模型优先，其余按场景排名为 fallback
            chain = self.selector.select_chain(scene, model=model)
        elif scene:
            # 仅场景：按场景综合分排序（不让 default 抢序）
            chain = self.selector.select_chain(scene)
        else:
            # 都没给：default_model 强制优先，其余按综合分兜底
            dm = self.cfg.default_model
            rest = self.selector.select_chain(None, exclude={dm})
            chain = [dm] + rest
        if not chain:
            raise LLMCallFailed("无可用模型（所有候选熔断或未配置）")

        last_err: Exception | None = None
        for mid in chain:
            h = self.health.get(mid) or ModelHealth(mid)
            if not h.can_use():                    # 已熔断则跳过
                continue
            # 缓存（按已解析模型 + prompt + 温度）
            if self.cache is not None:
                cached = self.cache.get(mid, prompt, temp)
                if cached is not None:
                    return cached
            try:
                ad = self._adapter_for(mid)
                resp = ad.complete(prompt, model=mid, temperature=temp, tag=tag)
            except LLMBudgetExceeded:
                raise
            except Exception as exc:
                h.record_failure(str(exc))
                last_err = exc
                logger.warning("模型 %s 调用失败，尝试候选链下一个: %s", mid, exc)
                continue
            # 成功
            h.record_success(resp.latency_ms)
            self.cost.record(resp, tag=tag)
            if self.audit is not None:
                try:
                    self.audit({
                        "prompt_hash": _key(mid, prompt, temp),
                        "model": mid, "node": tag or None,
                        "prompt": prompt, "response": resp.content,
                        "input_tokens": resp.prompt_tokens,
                        "output_tokens": resp.completion_tokens,
                        "cost_cny": resp.cost_cny, "latency_ms": resp.latency_ms,
                    })
                except Exception as exc:
                    logger.warning("LLM 审计落库失败（不影响主流程）: %s", exc)
            if self.cache is not None:
                self.cache.put(mid, prompt, temp, resp)
            return resp

        raise LLMCallFailed(f"所有候选模型均失败: {last_err}") from last_err

    # ---- 可观测 ----
    def health_snapshot(self) -> list[dict]:
        return [h.snapshot() for h in self.health.values()]


__all__ = ["LLMManager"]
