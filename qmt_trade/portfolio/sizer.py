"""仓位管理（设计 6.7）：风险预算法。

核心思想：**每笔交易允许亏损的金额恒定，而不是买入金额恒定**。
高波动股止损距离大 → 自动少买；低波动股 → 多买。解决了 qmt_etf
"高波动和低波动买一样多"的隐患。

Step: 基础预算 → 置信度调整 → Regime 调整 → 按止损距离反推股数
      → 多重上限裁剪（单票权重/可用资金/流动性/组合剩余额度）→ A股取整
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.trading import Side
from ..brain.schemas import TradeIntent
from ..features.regime import Regime

_CONVICTION_FACTOR = {"LOW": 0.6, "MEDIUM": 1.0, "HIGH": 1.4}
_REGIME_FACTOR = {
    Regime.TREND_UP: 1.0, Regime.RANGE: 0.7,
    Regime.TREND_DOWN: 0.4, Regime.RISK_OFF: 0.0,
}


@dataclass
class SizingContext:
    total_asset: float
    available_cash: float
    entry_price: float
    stop_price: float
    avg_volume_20d: float          # 近 20 日均成交量（股）
    regime: Regime
    current_weight: float = 0.0    # 该票当前权重（加仓场景）
    industry: str = ""
    #: 组合现有风险敞口占比（Σ(现价-止损)·股数 / 总资产），用于组合层约束
    portfolio_risk_used: float = 0.0
    #: 组合剩余可加风险预算（占总资产比例）
    portfolio_risk_remain: float = 1.0


@dataclass
class SizingResult:
    shares: int
    risk_amount: float
    stop_distance: float
    reasons: list[str] = field(default_factory=list)
    bounded_by: str = ""           # 哪个上限最终裁剪

    def ok(self) -> bool:
        return self.shares >= 100


class PositionSizer:
    def __init__(self, settings):
        cfg = settings.section("portfolio")
        self.base_risk_pct = float(cfg.get("base_risk_pct", 0.006))
        self.max_weight_pct = float(cfg.get("max_weight_pct", 0.15))
        self.max_single_cash_pct = float(cfg.get("max_order_value_ratio", 0.10))
        self.liquidity_ratio = float(cfg.get("max_volume_ratio_of_adv", 0.05))
        self.cash_buffer = float(cfg.get("cash_usage_ratio", 0.95))
        self.cash_buffer = 1.0 - self.cash_buffer  # cash_usage_ratio 是"使用比例"，缓冲=1-使用
        self.portfolio_risk_budget = float(cfg.get("total_risk_budget", 0.03))
        self.floor_shares = int(cfg.get("min_shares", 100))
        # 等权篮子（2026-08-13 P1 分散度）：开启后每只目标 ~1/target_positions，
        # 稀释单票利润集中（项目铁律：单票贡献>50% 视为特异性运气而非稳健策略）。
        # 仍受 max_weight_pct / cash / 流动性 / 组合剩余风险额度约束；高波动个股由
        # portfolio_risk_budget 护栏兜底，避免等权把高波动票也买满。
        self.equal_weight = bool(cfg.get("equal_weight", False))
        self.target_positions = max(1, int(cfg.get("target_positions", 8)))

    def suggest(self, intent: TradeIntent, ctx: SizingContext) -> SizingResult:
        if ctx.entry_price <= 0 or ctx.stop_price >= ctx.entry_price:
            return SizingResult(0, 0.0, 0.0, reasons=["入场价/止损价非法"])

        # Step 1-3 风险预算
        risk = ctx.total_asset * self.base_risk_pct
        risk *= _CONVICTION_FACTOR.get(intent.conviction, 1.0)
        risk *= _REGIME_FACTOR.get(ctx.regime, 0.7)
        rf = intent.risk_budget_hint
        # Intent 的风险预算提示（百分比 0.3-1.5 即 0.3%-1.5%）作为硬上限，防止 LLM 过度激进
        risk = min(risk, ctx.total_asset * max(0.0, rf) / 100.0)
        if risk <= 0:
            return SizingResult(0, 0.0, 0.0, reasons=["Regime 或置信度导致预算为 0"])

        stop_dist = ctx.entry_price - ctx.stop_price
        if stop_dist <= 0:
            return SizingResult(0, 0.0, 0.0, reasons=["止损距离非正"])

        raw_shares = risk / stop_dist
        reasons = [f"风险预算={risk:.0f} 止损距离={stop_dist:.3f}"]

        # 等权篮子（P1 分散度）：强制每只目标 ~1/target_positions，稀释单票利润集中。
        # 风险预算路径仍作为下限护栏保留——高波动个股（止损距离大）的等权 notional
        # 会被 portfolio_risk_budget 折算的上限压回，避免等权把高波动票也买满。
        if self.equal_weight and stop_dist > 0:
            target_w = min(1.0 / self.target_positions, self.max_weight_pct)
            risk_guard_notional = (ctx.total_asset * self.portfolio_risk_budget) / stop_dist
            target_notional = min(ctx.total_asset * target_w, risk_guard_notional)
            raw_shares = target_notional / ctx.entry_price
            reasons.append(f"等权目标=每只~{target_w:.1%}")

        # Step 5 多重上限裁剪（取最小）
        bounded = "equal_weight" if self.equal_weight else "risk_budget"
        cap = raw_shares

        w_cap = (ctx.total_asset * self.max_weight_pct / ctx.entry_price)
        if w_cap < cap:
            cap, bounded = w_cap, "max_weight"
        cash_cap = (ctx.available_cash * (1 - self.cash_buffer)) / ctx.entry_price
        if cash_cap < cap:
            cap, bounded = cash_cap, "available_cash"
        liq_cap = ctx.avg_volume_20d * self.liquidity_ratio
        if liq_cap < cap:
            cap, bounded = liq_cap, "liquidity"
        # 组合层剩余风险额度
        remain_notional = (ctx.portfolio_risk_remain * ctx.total_asset) / stop_dist if stop_dist else 0
        if remain_notional < cap:
            cap, bounded = remain_notional, "portfolio_risk"

        # Step 6 A股取整到 100
        shares = int(cap // 100 * 100)
        if shares < self.floor_shares:
            return SizingResult(0, risk, stop_dist, reasons=reasons + [f"取整后不足 {self.floor_shares} 股，放弃"],
                                 bounded_by=bounded)
        return SizingResult(int(shares), risk, stop_dist, reasons=reasons, bounded_by=bounded)
