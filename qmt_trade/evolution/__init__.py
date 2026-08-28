"""L5 进化层：复盘归因 → 经验库 → walk-forward 寻优 → 策略池调权。

这是 TradingAgents-CN 缺失最严重的一层（它的 ``reflect_and_remember()``
是死代码）。此处三个模块彼此解耦但可串成闭环：

    ReviewEngine（今天亏在哪）
        → factor_ic / lessons
        → WalkForwardOptimizer（参数该不该动、动多少）
        → StrategyPool（钱该给谁）
"""

from .optimizer import OptimizeResult, ParamScore, WalkForwardOptimizer, Window
from .pool import (CASH, PoolDecision, PoolMetrics, RebalanceResult,
                   StrategyPool, StrategyRecord)
from .review import Lesson, ReviewEngine, ReviewResult, TradeAttribution

__all__ = [
    "ReviewEngine", "ReviewResult", "TradeAttribution", "Lesson",
    "WalkForwardOptimizer", "OptimizeResult", "ParamScore", "Window",
    "StrategyPool", "StrategyRecord", "PoolMetrics", "PoolDecision",
    "RebalanceResult", "CASH",
]
