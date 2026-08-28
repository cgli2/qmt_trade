"""模型健康度追踪（熔断 + 成功率），供智能选模打分。

设计取舍：每个模型独立记成功/失败，连续失败达到阈值自动熔断一段时间；
熔断中的模型在 ``ModelSelector`` 排序时直接出局，调用方自动切到候选链下一个。
这是「失败安全（P4）」在 LLM 层的落地——坏模型被自动降级而非拖垮整个研判。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class ModelHealth:
    model_id: str
    total: int = 0
    success: int = 0
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0       # epoch seconds；>now 表示熔断中
    last_error: str = ""
    last_latency_ms: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def success_rate(self) -> float:
        return (self.success / self.total) if self.total else 1.0

    def is_circuit_open(self, now: float | None = None) -> bool:
        now = now or time.time()
        return self.circuit_open_until > now

    def can_use(self, *, cooldown: float = 300.0, fail_threshold: int = 5) -> bool:
        """是否可用；若连续失败超阈值则自动拉闸熔断。"""
        if self.is_circuit_open():
            return False
        if self.consecutive_failures >= fail_threshold:
            with self._lock:
                self.circuit_open_until = time.time() + cooldown
            return False
        return True

    def record_success(self, latency_ms: int = 0) -> None:
        with self._lock:
            self.total += 1
            self.success += 1
            self.consecutive_failures = 0
            self.last_latency_ms = latency_ms

    def record_failure(self, error: str = "") -> None:
        with self._lock:
            self.total += 1
            self.consecutive_failures += 1
            self.last_error = error

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "model": self.model_id,
                "total": self.total,
                "success": self.success,
                "success_rate": round(self.success_rate, 4),
                "consecutive_failures": self.consecutive_failures,
                "circuit_open": self.is_circuit_open(),
                "last_error": self.last_error,
            }


__all__ = ["ModelHealth"]
