"""组合经理（设计 6.4.2）。

TradingAgents-CN 完全缺失的一层：它的 ``company_of_interest`` 是单个字符串，
一次只看一只票，永远不知道"我已经持有 3 只同行业的了"。

这里把 ``PortfolioSnapshot`` 作为一等输入，做四件事：

1. 已持有该标的 → 转为加仓/持有判断，避免重复建仓；
2. 行业集中度超标 → 直接降 conviction 或否决；
3. 持仓数/总仓位触顶 → 否决新开仓；
4. 现金不足 → 否决。

注意：这里是**建议层**，最终硬约束仍由 L3 RiskEngine 执行（P1：LLM 无权绕过风控）。
"""

from __future__ import annotations

from ..state import AgentState, AnalystReport
from .base import Agent, stance_of


class PortfolioManager(Agent):
    name = "portfolio_manager"
    scene = "market_analysis"            # 组合决策场景

    def __init__(self, client=None, *, use_llm: bool = False, model: str | None = None,
                 max_industry_weight: float = 0.30, min_cash_ratio: float = 0.05,
                 max_new_per_day: int = 5):
        super().__init__(client, model=model, use_llm=use_llm)
        self.max_industry_weight = max_industry_weight
        self.min_cash_ratio = min_cash_ratio
        self.max_new_per_day = max_new_per_day

    def _rule_based(self, state: AgentState) -> AnalystReport:
        pf = state.portfolio
        score = state.reports.get("research_manager")
        base = score.score if score else state.bull_score
        risks: list[str] = []
        highlights: list[str] = []

        held_w = pf.position_weight.get(state.symbol, 0.0)
        if held_w > 0:
            highlights.append(f"已持有 {held_w:.1%}，本次视为加仓评估")
            state.votes["already_held"] = f"{held_w:.4f}"
            if held_w >= 0.9 * min(0.30, pf.max_position_pct):
                risks.append("该标的权重已接近单票上限，不宜再加")
                base = min(base, 0.45)

        ind = state.industry or "未知"
        ind_w = pf.industry_weight.get(ind, 0.0)
        if ind_w >= self.max_industry_weight:
            risks.append(f"行业 {ind} 暴露已达 {ind_w:.1%}（上限 {self.max_industry_weight:.0%}）")
            base = min(base, 0.40)
            state.risk_flags.append("INDUSTRY_CONCENTRATION")
        elif ind_w >= self.max_industry_weight * 0.7:
            risks.append(f"行业 {ind} 暴露 {ind_w:.1%} 偏高")
            base *= 0.92

        if pf.n_positions >= pf.max_positions and held_w <= 0:
            risks.append(f"持仓数已满 {pf.n_positions}/{pf.max_positions}")
            state.veto_reason = state.veto_reason or "持仓数已满"
            base = 0.0
        if pf.gross_weight >= pf.max_position_pct and held_w <= 0:
            risks.append(f"总仓位 {pf.gross_weight:.1%} 已达 Regime 上限 {pf.max_position_pct:.0%}")
            state.veto_reason = state.veto_reason or "总仓位触顶"
            base = 0.0
        if pf.total_asset > 0 and pf.cash / pf.total_asset < self.min_cash_ratio:
            risks.append(f"现金比例 {pf.cash / pf.total_asset:.1%} 低于下限")
            base = min(base, 0.35)
        if pf.opened_today >= self.max_new_per_day:
            risks.append(f"当日新开仓已达 {pf.opened_today}")
            state.veto_reason = state.veto_reason or "当日开仓数已满"
            base = 0.0
        if pf.drawdown <= -0.10:
            risks.append(f"组合回撤 {pf.drawdown:.1%}，应降低新增风险敞口")
            base *= 0.85

        return AnalystReport(
            agent=self.name, stance=stance_of(base), score=round(max(0.0, base), 4),
            confidence=0.9, highlights=highlights, risks=risks,
        )

    def _prompt(self, state: AgentState) -> str:
        if not self.use_llm:
            return ""
        return (
            f"SYMBOL: {state.symbol}\n"
            f"你是组合经理。判断在当前组合状态下，新增/加仓该标的是否合适。\n"
            f"重点考虑：与现有持仓的行业相关性、集中度、资金占用、回撤状态。\n"
            f"--- 组合状态 ---\n{state.portfolio.render()}\n"
            f"--- 投资论点 ---\n{state.thesis}\n"
            f'严格输出 JSON: {{"score": 0~1 适合程度, "confidence": 0~1, '
            f'"risks": ["组合层面风险"]}}\n'
        )
