"""OpenAI 兼容协议适配器（DeepSeek / 通义千问 / OpenAI / 任意兼容网关）。

现在主流国产模型都提供 OpenAI 兼容的 ``/chat/completions`` 端点，所以一个
adapter 就够了，换模型只改 ``base_url`` + ``api_key`` + ``model``。

刻意**不依赖 openai SDK**，只用 ``requests``：

* 少一个会跟着大版本乱改接口的依赖；
* 请求/重试/超时全部握在自己手里，行为可预测；
* 出问题时看得懂发出去的到底是什么。

失败处理遵循 P4/P5：

* 网络类错误（超时、5xx、429）按指数退避重试，重试仍失败 → 抛
  :class:`LLMCallFailed`，由 Agent 层捕获后**降级到规则路径**，不影响交易闭环；
* 4xx（除 429）属于配置错误，重试没意义，立即失败；
* 成本按 ``llm.price_per_1k_tokens`` 配置表折算成**人民币**，交给
  :class:`CostTracker` 熔断。
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from ...core.errors import LLMCallFailed
from .base import LLMAdapter, LLMResponse

logger = logging.getLogger(__name__)

#: 值得重试的 HTTP 状态：限流 + 服务端抖动
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: 内置兜底价（元 / 1k tokens）。配置表里没有的模型用它，避免"成本算成 0
#: 于是永远不熔断"这种最危险的情况。
_FALLBACK_PRICE = {"input": 0.01, "output": 0.03}


class OpenAILikeAdapter(LLMAdapter):
    """通用 OpenAI 兼容客户端。

    :param base_url: 形如 ``https://api.deepseek.com/v1``
    :param api_key: 密钥，从 ``Secrets`` 取，**绝不写进配置文件**
    :param default_model: 未显式指定 model 时使用
    :param prices: ``{model: {"input": 元/1k, "output": 元/1k}}``
    """

    def __init__(self, *, base_url: str, api_key: str, default_model: str,
                 prices: dict[str, dict] | None = None, timeout: float = 60.0,
                 max_retries: int = 2, name: str = "openai_like",
                 extra_headers: dict[str, str] | None = None):
        if not api_key:
            raise LLMCallFailed(f"{name}: 缺少 API Key，请在 secrets 中配置")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.prices = prices or {}
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.name = name
        self.extra_headers = extra_headers or {}
        self._session = None                          # 懒建，避免 import 期就要 requests

    # ------------------------------------------------------------- 内部
    def _sess(self):
        if self._session is None:
            import requests                            # 局部 import：没装也不影响 Mock 模式
            s = requests.Session()
            s.headers.update({
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                **self.extra_headers,
            })
            self._session = s
        return self._session

    def price_of(self, model: str) -> dict:
        p = self.prices.get(model)
        if not p:
            logger.warning("模型 %s 无价格配置，按兜底价计费（宁可高估也不能算成 0）", model)
            return _FALLBACK_PRICE
        return p

    def _cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        p = self.price_of(model)
        return (prompt_tokens / 1000.0 * float(p.get("input", 0))
                + completion_tokens / 1000.0 * float(p.get("output", 0)))

    @staticmethod
    def _sleep(attempt: int) -> None:
        """指数退避 + 抖动。抖动是为了避免多个分析师线程同时重试再次撞限流。"""
        time.sleep(min(8.0, 0.5 * (2 ** attempt)) * (0.7 + 0.6 * random.random()))

    # ------------------------------------------------------------- 主流程
    def complete(self, prompt: str, *, model: str | None = None,
                 temperature: float = 0.0, **kwargs) -> LLMResponse:
        model = model or self.default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(temperature),
            "stream": False,
        }
        if kwargs.get("max_tokens"):
            payload["max_tokens"] = int(kwargs["max_tokens"])
        if kwargs.get("json_mode"):
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.base_url}/chat/completions"
        t0 = time.perf_counter()
        last = ""

        for attempt in range(self.max_retries + 1):
            try:
                resp = self._sess().post(url, json=payload, timeout=self.timeout)
            except Exception as exc:                  # 连接/超时类，值得重试
                last = f"{type(exc).__name__}: {exc}"
                logger.warning("LLM 请求异常（第 %d 次）: %s", attempt + 1, last)
                if attempt < self.max_retries:
                    self._sleep(attempt)
                    continue
                raise LLMCallFailed(f"{self.name} 请求失败：{last}") from exc

            if resp.status_code in _RETRYABLE_STATUS:
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning("LLM 可重试错误（第 %d 次）: %s", attempt + 1, last)
                if attempt < self.max_retries:
                    self._sleep(attempt)
                    continue
                raise LLMCallFailed(f"{self.name} 重试耗尽：{last}")

            if resp.status_code >= 400:
                # 401/403/404/422：改配置才能解决，重试只是浪费时间和配额
                raise LLMCallFailed(
                    f"{self.name} 请求被拒（不可重试）HTTP {resp.status_code}: "
                    f"{resp.text[:200]}")

            try:
                data = resp.json()
            except Exception as exc:
                raise LLMCallFailed(f"{self.name} 响应非 JSON：{resp.text[:200]}") from exc

            return self._to_response(data, model, prompt, t0)

        raise LLMCallFailed(f"{self.name} 未知失败：{last}")   # pragma: no cover

    def _to_response(self, data: dict, model: str, prompt: str, t0: float) -> LLMResponse:
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMCallFailed(
                f"{self.name} 响应结构异常：{json.dumps(data, ensure_ascii=False)[:200]}"
            ) from exc

        usage = data.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        if pt == 0 and ct == 0:
            # 有些兼容网关不回 usage。粗估也比记 0 强——记 0 等于关掉了成本熔断。
            pt, ct = len(prompt) // 2, len(content) // 2
            logger.debug("%s 未返回 usage，按字符数粗估 tokens", self.name)

        return LLMResponse(
            content=content,
            model=data.get("model") or model,
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_cny=self._cost(model, pt, ct),
            cached=False,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            meta={"finish_reason": (data.get("choices") or [{}])[0].get("finish_reason"),
                  "provider": self.name},
        )

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:                          # pragma: no cover
                pass
            self._session = None


__all__ = ["OpenAILikeAdapter"]
