"""按 ProviderConfig 构建 LLMAdapter；便捷装配 LLMManager。

设计：
- ``build_adapter``：provider 为 mock 类型 / 未配置 API Key 时静默退回 ``MockLLM``
  （P5：没接真模型系统也能跑，成本 0，不抛错）；
- ``build_llm_manager``：从 ``config/llm.yaml`` 装配 ``LLMManager``（延迟导入 manager 避免循环依赖）。
"""

from __future__ import annotations

from .base import LLMAdapter
from .mock import MockLLM
from .openai_like import OpenAILikeAdapter
from .registry import ProviderConfig, ModelConfig


def build_adapter(provider: ProviderConfig | None, model: ModelConfig | None) -> LLMAdapter:
    """按 provider 配置构建 adapter。无 key / mock 类型静默退 MockLLM（P5 不崩）。"""
    if provider is None or provider.is_mock:
        return MockLLM(latency_ms=0)
    key = provider.api_key
    if not key:
        # 未配置 API Key：降级 mock，成本 0，不抛错（系统继续跑纯因子增强）
        return MockLLM(latency_ms=0)
    return OpenAILikeAdapter(
        base_url=provider.base_url,
        api_key=key,
        default_model=model.name if model else "",
        prices=dict(model.price_per_1k_tokens) if model else {},
        timeout=provider.timeout,
        max_retries=provider.max_retries,
        name=provider.id,
        extra_headers=dict(provider.extra_headers),
    )


def build_llm_manager(path=None) -> "LLMManager":
    """从 config/llm.yaml 装配 LLMManager（延迟导入避免循环依赖）。"""
    from .manager import LLMManager
    return LLMManager.from_file(path)


__all__ = ["build_adapter", "build_llm_manager"]
