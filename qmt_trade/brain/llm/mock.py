"""MockLLM：确定性假模型，用于测试与 P5 纯因子降级。

不调用任何外部 API。给定候选标的，返回一个**结构合法**的 TradeIntent 草稿
（实际决策仍由 RiskEngine 把关，LLM 只给建议）。这样没接真模型也能跑通 L2-b。
"""

from __future__ import annotations

import hashlib
import time

from .base import LLMAdapter, LLMResponse
from ..schemas import TradeIntent, intent_from_rank


class MockLLM(LLMAdapter):
    name = "mock"

    def __init__(self, *, latency_ms: int = 5):
        self.latency_ms = latency_ms

    def complete(self, prompt: str, *, model: str | None = None,
                 temperature: float = 0.0, **kwargs) -> LLMResponse:
        # 根据 prompt 内容做确定性"决策"，避免随机（可复现）
        h = hashlib.sha256(prompt.encode()).hexdigest()
        seed = int(h[:8], 16)
        conv = ["LOW", "MEDIUM", "HIGH"][seed % 3]
        stop = [0.05, 0.07, 0.10][seed % 3]
        # 把 prompt 里能解析出的 symbol 截出来（decide.py 会在 prompt 注入 SYMBOL:）
        sym = "MOCK"
        for line in prompt.splitlines():
            if line.startswith("SYMBOL:"):
                sym = line.split(":", 1)[1].strip()
        content = (
            f'{{"symbol":"{sym}","action":"BUY","confidence":0.7,'
            f'"conviction":"{conv}","stop_loss_type":"FIXED_PCT",'
            f'"stop_loss_value":{stop},"risk_budget_hint":1.0,'
            f'"max_weight_hint":0.12,"reasoning":"mock 研判：因子入选"}}'
        )
        time.sleep(self.latency_ms / 1000.0)
        return LLMResponse(
            content=content, model=model or "mock",
            prompt_tokens=len(prompt) // 2, completion_tokens=len(content) // 2,
            cost_cny=0.0, cached=False, latency_ms=self.latency_ms,
            meta={"symbol": sym, "mock": True},
        )
