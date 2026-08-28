"""LLM 适配层抽象（设计 6.4）。

所有 LLM 调用都走这个接口，方便替换模型 / 接 Mock / 做成本熔断。
P5 的价值：``MockLLM`` 让系统在没接真模型时也能完整跑（纯因子模式）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    cached: bool = False
    latency_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


class LLMAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def complete(self, prompt: str, *, model: str | None = None,
                 temperature: float = 0.0, **kwargs) -> LLMResponse:
        raise NotImplementedError

    def embed(self, text: str) -> list[float] | None:
        """可选：语义检索用。默认不支持。"""
        return None
