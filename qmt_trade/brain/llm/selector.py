"""场景化智能选模（评分 + 熔断感知 + 降级链）。

相对 ai-dev-ops「取第一个 capability 匹配」的改进：
- capability 匹配度（prefer 标签命中率）
- health（成功率、是否熔断、连续失败惩罚）
- cost（单价越低分越高）
三者加权 → 候选模型排序；``LLMManager`` 按排序链依次尝试，失败自动下一个。

选模是「软」的：它给出优先级，真正的失败兜底在 manager 的 fallback 链里。
"""

from __future__ import annotations

from .registry import LLMConfig, ModelConfig, SceneConfig
from .health import ModelHealth


class ModelSelector:
    def __init__(self, cfg: LLMConfig, health: dict[str, ModelHealth] | None = None):
        self.cfg = cfg
        self.health: dict[str, ModelHealth] = health or {}

    def health_of(self, model_id: str) -> ModelHealth:
        h = self.health.get(model_id)
        if h is None:
            h = ModelHealth(model_id)
            self.health[model_id] = h
        return h

    # ---- 子分 ----
    def _capability_score(self, model: ModelConfig, scene: SceneConfig | None) -> float:
        if scene is None or not scene.prefer:
            return 1.0
        if not model.capabilities:
            return 0.5
        hits = sum(1 for c in scene.prefer if c in model.capabilities)
        return hits / len(scene.prefer)

    def _health_score(self, model_id: str) -> float:
        h = self.health_of(model_id)
        if h.is_circuit_open():
            return 0.0
        sr = h.success_rate
        penalty = min(1.0, h.consecutive_failures / 5.0)
        return max(0.0, sr * (1.0 - 0.5 * penalty))

    def _cost_score(self, model: ModelConfig) -> float:
        inp = model.price("input")
        out = model.price("output")
        if inp <= 0 and out <= 0:
            return 0.5
        avg = (inp + out) / 2.0
        # 0.03 元/1k 视为贵(0分)，≤0.001 视为便宜(1分)
        return max(0.0, min(1.0, 1.0 - avg / 0.03))

    def _score(self, model_id: str, scene: SceneConfig | None) -> float:
        model = self.cfg.get_model(model_id)
        if model is None:
            return -1.0
        sel = self.cfg.selection
        cap = self._capability_score(model, scene)
        hth = self._health_score(model_id)
        cst = self._cost_score(model)
        if sel.strategy == "capability_first":
            # 能力优先：能力分不过半直接出局，再比健康与成本
            if cap < 0.5:
                return -1.0
            return sel.health_weight * hth + sel.cost_weight * cst
        return (sel.capability_weight * cap
                + sel.health_weight * hth
                + sel.cost_weight * cst)

    # ---- 排序 / 选链 ----
    def rank(self, scene_id: str | None = None, *,
             exclude: set[str] | None = None) -> list[str]:
        """返回某场景下按综合分降序的模型 id 列表（熔断中的模型直接出局）。"""
        exclude = exclude or set()
        scene = self.cfg.get_scene(scene_id) if scene_id else None
        candidates = self.cfg.candidates_for_scene(scene_id)
        if not scene_id:
            # 无场景：default_model 优先，其余按综合分
            dm = self.cfg.default_model
            if dm in candidates:
                candidates = [dm] + [c for c in candidates if c != dm]
        scored = []
        for mid in candidates:
            if mid in exclude:
                continue
            h = self.health_of(mid)
            # 已熔断 或 连续失败达阈值：直接出局（避免把请求发给已知坏模型）
            if h.is_circuit_open() or h.consecutive_failures >= 5:
                continue
            s = self._score(mid, scene)
            if s < 0:
                continue
            scored.append((s, mid))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [mid for _, mid in scored]

    def select_chain(self, scene_id: str | None = None, *,
                     model: str | None = None,
                     exclude: set[str] | None = None) -> list[str]:
        """返回调用方应按顺序尝试的模型链。

        - 显式 ``model``：该模型排第一，余下按场景排序补为 fallback；
        - 仅 ``scene``：按综合分排序整条链；
        - 都没有：返回全部模型（按默认顺序）。
        """
        exclude = set(exclude or set())
        if model:
            chain = [model]
            rest = self.rank(scene_id, exclude=exclude | {model})
            return chain + rest
        return self.rank(scene_id, exclude=exclude)


__all__ = ["ModelSelector"]
