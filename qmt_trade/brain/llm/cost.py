"""LLM 成本追踪与硬性熔断（设计 6.4.4）。

修正 TradingAgents-CN 的 ``COST_ALERT_THRESHOLD`` 只告警不阻断：这里超过预算
直接停用 LLM 层（抛 ``LLMBudgetExceeded``），系统自动降级为纯因子模式（P5 的价值）。

.. note::

   **全系统成本一律以人民币（元）计价**。配置里的 ``llm.price_per_1k_tokens``
   和 ``llm.budget.*_cny`` 都是元，运维告警和日报也都按元展示。历史上这里的字段
   叫过 ``*_usd`` 而值却是元，导致熔断线（当美元用的 5.0）和体检线（30 元）
   对不上——预算类字段的单位必须写进名字里，不能靠注释。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class CostTracker:
    """成本累计 + 硬熔断。

    多个分析师在线程池里并发调用 LLM，累加与判阈必须加锁——否则可能出现
    "两个线程同时读到未超预算、一起放行"的漏判，钱就花超了。
    """

    daily_budget_cny: float = 30.0
    monthly_budget_cny: float = 600.0
    _day: str = ""
    _day_cost: float = 0.0
    _month: str = ""
    _month_cost: float = 0.0
    history: list[dict] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def _rollover(self) -> None:
        t = time.localtime()
        day = f"{t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d}"
        month = f"{t.tm_year}-{t.tm_mon:02d}"
        if day != self._day:
            self._day, self._day_cost = day, 0.0
        if month != self._month:
            self._month, self._month_cost = month, 0.0

    def record(self, resp, *, tag: str = "") -> None:
        with self._lock:
            self._rollover()
            self._day_cost += resp.cost_cny
            self._month_cost += resp.cost_cny
            self.history.append({
                "ts": time.time(), "cost": resp.cost_cny, "model": resp.model,
                "tag": tag, "cached": resp.cached,
            })

    def check(self) -> None:
        """超限即抛 LLMBudgetExceeded（由调用方捕获 → 降级纯因子模式）。"""
        from ...core.errors import LLMBudgetExceeded
        with self._lock:
            self._rollover()
            day, month = self._day_cost, self._month_cost
        if day > self.daily_budget_cny:
            raise LLMBudgetExceeded(
                f"日 LLM 成本 ¥{day:.2f} 超预算 ¥{self.daily_budget_cny:.2f}")
        if month > self.monthly_budget_cny:
            raise LLMBudgetExceeded(
                f"月 LLM 成本 ¥{month:.2f} 超预算 ¥{self.monthly_budget_cny:.2f}")

    @property
    def day_cost(self) -> float:
        with self._lock:
            self._rollover()
            return self._day_cost

    @property
    def month_cost(self) -> float:
        with self._lock:
            self._rollover()
            return self._month_cost
