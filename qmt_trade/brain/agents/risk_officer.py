"""风险官（设计 6.4.2 末节）—— 最终产出 TradeIntent。

设计 6.4.1 的取舍：**风控以规则引擎为主，LLM 只补充规则覆盖不到的风险**。
所以这里 LLM 只能做两件事：
  1. 往 ``risk_flags`` 里加标记；
  2. 收紧（而非放宽）止损/风险预算。

LLM **永远不能**放大仓位或放宽止损——这是 P1/P3 的底线，用代码而不是 prompt 保证。
"""

from __future__ import annotations

from datetime import date, timedelta

from ..schemas import Evidence, TPLevel, TradeIntent
from ..state import AgentState, AnalystReport
from .base import Agent, stance_of

#: conviction 分档阈值（研究主管的综合分）
_CONV_HI, _CONV_MID = 0.68, 0.55


class RiskOfficer(Agent):
    name = "risk_officer"
    scene = "risk_assess"               # 风控评估场景

    def __init__(self, client=None, *, use_llm: bool = False, model: str | None = None,
                 base_stop_pct: float = 0.045, max_holding_days: int = 20,
                 valid_days: int = 3):
        super().__init__(client, model=model, use_llm=use_llm)
        self.base_stop_pct = base_stop_pct
        self.max_holding_days = max_holding_days
        self.valid_days = valid_days

    def _rule_based(self, state: AgentState) -> AnalystReport:
        rm = state.reports.get("research_manager")
        pm = state.reports.get("portfolio_manager")
        score = min(rm.score if rm else state.bull_score,
                    pm.score if pm else 1.0)
        risks: list[str] = []

        # 波动越大止损越宽（否则高波动票天天被扫止损），但设上下限
        # 注意：atr_ratio 因子在特征层是「取负」存储的（波动越小分越高），
        # 这里要还原成真实的 ATR/收盘 比例，否则止损永远退化成固定值。
        atr_raw = state.factpack.numerics.get("ATR占价比")
        stop = self.base_stop_pct
        if atr_raw is not None and abs(float(atr_raw)) > 1e-6:
            atr = abs(float(atr_raw))
            stop = max(0.04, min(0.12, 2.2 * atr))
            risks.append(f"按 ATR({atr:.3f}) 设止损 {stop:.1%}")

        dd = state.factpack.numerics.get("60日最大回撤")
        if dd is not None and dd <= -0.30:
            risks.append(f"60日回撤 {dd:.1%} 过深，趋势不稳")
            state.risk_flags.append("DEEP_DRAWDOWN")
            score *= 0.9
        if state.regime in ("TREND_DOWN", "RISK_OFF"):
            risks.append(f"市场环境 {state.regime}，压缩风险预算")
            score *= 0.85

        state.votes["risk_stop_pct"] = f"{stop:.4f}"
        return AnalystReport(
            agent=self.name, stance=stance_of(score), score=round(score, 4),
            confidence=0.85, risks=risks,
        )

    def _prompt(self, state: AgentState) -> str:
        if not self.use_llm:
            return ""
        return (
            f"SYMBOL: {state.symbol}\n"
            f"你是风险官。列举规则引擎可能遗漏的风险点，并给出可机器判定的失效条件。\n"
            f"你只能【收紧】风险（提高止损严格度 / 降低预算），不得放宽。\n"
            f"--- 投资论点 ---\n{state.thesis}\n"
            f"--- 事实卡片 ---\n{state.factpack.render()}\n"
            f'严格输出 JSON: {{"risk_flags": ["标签"], "invalidation": "论点失效的描述", '
            f'"invalidation_checks": ["score_percentile < 0.30"], '
            f'"stop_tighten_pct": 0~0.03 建议额外收紧的止损幅度}}\n'
        )

    def _merge_llm(self, base, data, raw, state):
        if isinstance(data.get("risk_flags"), list):
            state.risk_flags.extend(str(x) for x in data["risk_flags"][:5])
        if data.get("invalidation"):
            state.votes["invalidation"] = str(data["invalidation"])[:300]
        if isinstance(data.get("invalidation_checks"), list):
            state.votes["invalidation_checks"] = "|".join(
                str(x) for x in data["invalidation_checks"][:5])
        try:
            t = float(data.get("stop_tighten_pct", 0.0))
            if t > 0:  # 只允许收紧
                cur = float(state.votes.get("risk_stop_pct", self.base_stop_pct))
                state.votes["risk_stop_pct"] = f"{max(0.03, cur - min(0.03, t)):.4f}"
        except (TypeError, ValueError):
            pass
        base.raw = raw
        return base

    # ------------------------------------------------------------- 产出 Intent
    def make_intent(self, state: AgentState, *, min_score: float = 0.50) -> TradeIntent | None:
        """把全流程结论落成 TradeIntent。不达标或被否决 → 返回 None（不产生瞎猜的 Intent）。"""
        if state.veto_reason:
            return None
        rep = state.reports.get(self.name)
        score = rep.score if rep else state.bull_score
        if score < min_score:
            state.veto_reason = f"综合研判分 {score:.3f} < 门槛 {min_score:.2f}"
            return None

        conv = "HIGH" if score >= _CONV_HI else ("MEDIUM" if score >= _CONV_MID else "LOW")
        stop = float(state.votes.get("risk_stop_pct", self.base_stop_pct))
        budget = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.35}[conv]
        if state.regime in ("TREND_DOWN", "RISK_OFF"):
            budget *= 0.6

        checks = [c for c in state.votes.get("invalidation_checks", "").split("|") if c]
        if not checks:
            checks = ["score_percentile < 0.30", "regime == RISK_OFF",
                      f"close < entry * {1 - stop:.4f}"]

        evid = [
            Evidence(source="factor", ts=str(state.asof),
                     summary=f"综合分 {state.score:.4f}，排名 {state.rank}"),
            Evidence(source="factpack", ts=str(state.factpack.asof),
                     summary=f"事实覆盖率 {state.factpack.coverage:.0%}，"
                             f"字段 {len(state.factpack.facts)} 条"),
        ]
        for a, r in state.reports.items():
            state.votes[a] = f"{r.stance}:{r.score:.2f}"

        return TradeIntent(
            symbol=state.symbol,
            action="BUY",
            confidence=round(min(1.0, max(0.0, score)), 3),
            conviction=conv,  # type: ignore[arg-type]
            entry_type="LIMIT",
            entry_ref_price=state.ref_price or None,
            stop_loss_type="FIXED_PCT",
            stop_loss_value=round(stop, 4),
            take_profit=[
                TPLevel(price_or_pct=round(stop * 1.5, 4), ratio=0.4, kind="PCT"),
                TPLevel(price_or_pct=round(stop * 3.0, 4), ratio=0.6, kind="PCT"),
            ],
            risk_budget_hint=round(budget, 3),
            max_weight_hint=0.12,
            time_horizon_days=max(1, self.max_holding_days // 2),
            max_holding_days=self.max_holding_days,
            valid_until=state.asof + timedelta(days=self.valid_days),
            invalidation=state.votes.get(
                "invalidation", "综合分跌出全市场前 30% 或市场转 RISK_OFF 则论点失效"),
            invalidation_checks=checks,
            evidence=evid,
            reasoning=state.thesis[:800],
            risk_flags=sorted(set(state.risk_flags)),
            agent_votes=dict(state.votes),
            model_info={"llm_calls": state.llm_calls,
                        "cost_cny": round(state.llm_cost_cny, 6),
                        "degraded": ";".join(state.degraded)},
        )
