"""L2-b LLM 决策层的核心契约：TradeIntent（设计 6.4.3）。

这是 LLM 与下游风控/仓位之间唯一的"信使"。LLM **只**能输出这个结构，
无权直接下单、无权改仓位、无权绕过风控（P1/P3）。

为支持 P5（无 LLM 确定性闭环），这里同时提供 ``intent_from_rank()``：
根据选股排名直接合成一个确定性 Intent（conviction 按排名分档），
回测与"纯因子模式"走这条路径，LLM 层作为可选增强叠加在上方。
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..core.trading import Side


class TPLevel(BaseModel):
    """分批止盈的一个档位。"""
    price_or_pct: float                       # 绝对价 或 相对入场价的涨幅（由 kind 决定）
    ratio: float                              # 该档卖出占总计划仓位的百分比（0~1）
    kind: Literal["PRICE", "PCT"] = "PCT"


class Evidence(BaseModel):
    source: str
    ts: str
    summary: str


class TradeIntent(BaseModel):
    """LLM 发出的交易意图。缺失必填字段则 Intent 无效（由 factcheck 校验）。"""

    symbol: str
    action: Literal["BUY", "SELL", "HOLD", "REDUCE", "ADD"]
    confidence: float = Field(ge=0.0, le=1.0)
    conviction: Literal["LOW", "MEDIUM", "HIGH"]

    entry_type: Literal["MARKET_OPEN", "LIMIT", "BREAKOUT", "PULLBACK"] = "LIMIT"
    entry_ref_price: float | None = None
    entry_trigger: str | None = None

    stop_loss_type: Literal["FIXED_PCT", "ATR", "STRUCTURE"]
    stop_loss_value: float
    take_profit: list[TPLevel] = Field(default_factory=list)
    risk_budget_hint: float = Field(default=0.6, ge=0.0, le=1.5)   # 占总资产 %
    max_weight_hint: float = Field(default=0.10, ge=0.0, le=0.30)  # 单票权重上限 %

    time_horizon_days: int = 10
    max_holding_days: int = 20
    valid_until: date

    invalidation: str = ""
    invalidation_checks: list[str] = Field(default_factory=list)

    evidence: list[Evidence] = Field(default_factory=list)
    reasoning: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    agent_votes: dict[str, str] = Field(default_factory=dict)
    prompt_hash: str = ""
    model_info: dict[str, Any] = Field(default_factory=dict)

    @property
    def side(self) -> Side:
        return Side.BUY if self.action in ("BUY", "ADD") else Side.SELL

    def fingerprint(self) -> str:
        """内容指纹，用于缓存命中与回放溯源（不含时间戳等非确定性字段）。"""
        payload = f"{self.symbol}|{self.action}|{self.conviction}|{self.stop_loss_type}|" \
                  f"{self.stop_loss_value}|{self.risk_budget_hint}|{self.max_weight_hint}|" \
                  f"{self.max_holding_days}|{self.valid_until.isoformat()}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ----------------------------------------------------------------- 确定性合成
def intent_from_rank(
    symbol: str,
    *,
    rank: int,
    score: float,
    ref_price: float,
    stop_pct: float = 0.07,
    valid_until: date,
    industry: str = "",
    max_holding_days: int = 20,
    take_profit_pct: float = 0.0,
) -> TradeIntent:
    """回测/纯因子模式下，根据选股排名直接合成 Intent。

    conviction 按排名分档：前 1/3 HIGH、前 2/3 MEDIUM、其余 LOW。
    这样不需要 LLM 也能跑通完整风控→仓位→执行→回测闭环（P5）。

    ``take_profit_pct>0`` 时在建仓意图里挂一个**整仓止盈档**（V6 修复）：
    原纯因子路径 ``take_profit`` 恒为空，导致盈利持仓永远不被主动兑现、
    只有止损/时间止损能平仓 —— 下跌市里平仓盈亏几乎全是止损单，胜率结构性≈0%。
    挂上止盈后，盈利仓位到价即兑现，胜率才有意义、现金也能滚动到新选股。
    """
    conv = "HIGH" if rank <= max(1, 10) else ("MEDIUM" if rank <= max(1, 30) else "LOW")
    take_profit = (
        [TPLevel(price_or_pct=take_profit_pct, ratio=1.0, kind="PCT")]
        if take_profit_pct > 0
        else []
    )
    return TradeIntent(
        symbol=symbol,
        action="BUY",
        confidence=round(min(1.0, max(0.3, score)), 3),
        conviction=conv,  # type: ignore[arg-type]
        entry_type="LIMIT",
        entry_ref_price=ref_price,
        stop_loss_type="FIXED_PCT",
        stop_loss_value=stop_pct,
        take_profit=take_profit,
        risk_budget_hint={"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.35}[conv],
        max_weight_hint=0.12,
        time_horizon_days=max_holding_days // 2,
        max_holding_days=max_holding_days,
        valid_until=valid_until,
        invalidation=f"综合分跌破全市场后 30% 或 Regime 转 RISK_OFF",
        invalidation_checks=[f"score_percentile < 0.30", "regime == RISK_OFF"],
        reasoning=f"因子打分入选（rank={rank}, score={score:.3f}），行业={industry}",
    )
