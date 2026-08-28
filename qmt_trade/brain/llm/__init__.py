"""LLM 适配层（设计 6.4）。

对外统一入口：
- ``LLMManager.from_file()`` —— 多 provider / 多 model / 场景智能选模（推荐，生产用）；
- ``LLMClient(adapter, ...)`` —— 单 adapter 轻量包装（测试 / 简单场景）；
- ``load_llm_config`` / ``ModelSelector`` / ``ModelHealth`` —— 配置与选模原语。
"""

from __future__ import annotations

from .base import LLMAdapter, LLMResponse
from .mock import MockLLM
from .cache import LLMCache
from .cost import CostTracker
from .client import LLMClient
from .registry import (
    ProviderConfig, ModelConfig, SceneConfig, SelectionConfig,
    LLMConfig, load_llm_config,
)
from .health import ModelHealth
from .selector import ModelSelector
from .manager import LLMManager
from .factory import build_adapter, build_llm_manager

__all__ = [
    "LLMAdapter", "LLMResponse", "MockLLM", "LLMCache", "CostTracker",
    "LLMClient", "LLMManager", "ModelSelector", "ModelHealth",
    "ProviderConfig", "ModelConfig", "SceneConfig", "SelectionConfig",
    "LLMConfig", "load_llm_config", "build_adapter", "build_llm_manager",
]
