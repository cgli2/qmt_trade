"""四大分析师（设计 6.4.2 的并行 fan-out 层）。

每个分析师只看自己维度的事实，输出结构化 ``AnalystReport``。
规则路径直接用已算好的因子分位（确定性、零成本），LLM 路径做定性补充。

对 TradingAgents-CN 的改造：
- 串行 → 并行（由 graph.py 用线程池 fan-out）；
- ``len(report)>100`` 判完成 → 结构化字段校验；
- 分析师不再自己调工具取数（幻觉源头），只读 FactPack。
"""

from __future__ import annotations

import math

from ..state import AgentState, AnalystReport
from .base import Agent, stance_of


def _pct(state: AgentState, key: str, default: float = 0.5) -> float:
    """从 FactPack 数值索引取分位值。"""
    v = state.factpack.numerics.get(key)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return default
    return float(v)


def _describe(label: str, v: float) -> str:
    tag = "强" if v >= 0.7 else ("弱" if v <= 0.3 else "中性")
    return f"{label}分位 {v:.2f}（{tag}）"


class _FactorAnalyst(Agent):
    """共用逻辑：读某一类因子分位 → 分数 → 立场。"""

    scene = "market_analysis"            # 深度研判场景，由智能选模挑推理模型
    cat_key: str = ""
    detail_keys: tuple[str, ...] = ()
    prompt_role: str = ""

    def _rule_based(self, state: AgentState) -> AnalystReport:
        base = _pct(state, self.cat_key, 0.5)
        highlights, risks = [], []
        for k in self.detail_keys:
            v = state.factpack.numerics.get(k)
            if v is None:
                continue
            (highlights if v >= 0.65 else risks if v <= 0.35 else highlights).append(
                _describe(k, float(v)) if k.endswith("分位") else f"{k}={v:.4g}"
            )
        # 覆盖率低 → 信心打折（数据不足不能装作很确定）
        conf = 0.35 + 0.5 * state.factpack.coverage
        return AnalystReport(
            agent=self.name, stance=stance_of(base), score=round(base, 4),
            confidence=round(min(0.95, conf), 3),
            highlights=highlights[:4], risks=risks[:4],
        )

    def _prompt(self, state: AgentState) -> str:
        if not self.prompt_role:
            return ""
        lessons = ""
        if state.lessons:
            items = "\n".join(f"- {x}" for x in state.lessons[:6])
            lessons = (
                f"--- 近期复盘经验（必须纳入考量，与事实冲突时以事实为准） ---\n"
                f"{items}\n"
            )
        return (
            f"SYMBOL: {state.symbol}\n"
            f"你是{self.prompt_role}。只依据下面给出的事实作答，"
            f"事实中没有的数据一律视为未知，禁止臆测或引用外部记忆。\n"
            f"决策日: {state.asof}（只能使用该日期之前的信息）\n"
            f"市场环境: {state.regime}\n"
            f"--- 事实卡片 ---\n{state.factpack.render()}\n"
            f"{lessons}"
            f"--- 输出要求 ---\n"
            f'严格输出 JSON: {{"score": 0~1 看多程度, "confidence": 0~1, '
            f'"highlights": ["要点1","要点2"], "risks": ["风险1"]}}\n'
        )


class TechnicalAnalyst(_FactorAnalyst):
    name = "technical"
    cat_key = "动量类分位"
    detail_keys = ("20日涨跌幅", "60日涨跌幅", "12-1月动量", "均线多头排列分",
                   "距60日高点比", "20日乖离率", "60日最大回撤")
    prompt_role = "A股技术面分析师，关注趋势、动量、均线结构与回撤风险"


class FundamentalAnalyst(_FactorAnalyst):
    name = "fundamental"
    cat_key = "基本面类分位"
    detail_keys = ("ROE", "毛利率", "净利同比", "营收同比",
                   "盈利收益率(E/P)", "市净率倒数(B/P)", "偿债安全分")
    prompt_role = "A股基本面分析师，关注盈利质量、成长性与估值合理性"

    def _rule_based(self, state: AgentState) -> AnalystReport:
        r = super()._rule_based(state)
        # 基本面数据缺失率高时（A股财报季空窗），显式降信心而不是假装有结论
        miss = [k for k in self.detail_keys if k not in state.factpack.numerics]
        if len(miss) >= len(self.detail_keys) // 2:
            r.confidence = round(r.confidence * 0.6, 3)
            r.issues.append(f"基本面字段缺失 {len(miss)}/{len(self.detail_keys)}")
        return r


class MoneyFlowAnalyst(_FactorAnalyst):
    name = "moneyflow"
    cat_key = "资金类分位"
    detail_keys = ("近5日主力净流入", "近10日主力净流入", "主力净流入占成交比",
                   "大单占比", "资金流一致性", "换手率")
    prompt_role = "A股资金面分析师，关注主力资金动向、大单结构与流动性"


class SentimentAnalyst(_FactorAnalyst):
    name = "sentiment"
    cat_key = "情绪类分位"
    detail_keys = ("近5日新闻情绪", "近5日新闻热度", "近20日事件情绪", "所属行业动量")
    prompt_role = "A股消息面分析师，关注新闻情绪、事件驱动与行业景气"

    def _rule_based(self, state: AgentState) -> AnalystReport:
        r = super()._rule_based(state)
        # 硬负面事件：规则先行，直接否决，不等 LLM 慢慢想（设计 6.4）
        hn = state.factpack.numerics.get("硬负面事件标记", 0.0)
        if hn and float(hn) > 0:
            r.score, r.stance, r.confidence = 0.0, "BEAR", 0.95
            r.risks.insert(0, "存在硬负面事件（立案/退市风险/重大违规等）")
            state.risk_flags.append("HARD_NEGATIVE")
            state.veto_reason = "硬负面事件"
        return r


DEFAULT_ANALYSTS: tuple[type[_FactorAnalyst], ...] = (
    TechnicalAnalyst, FundamentalAnalyst, MoneyFlowAnalyst, SentimentAnalyst,
)


def build_analysts(client=None, *, use_llm: bool = True,
                   model: str | None = None) -> list[Agent]:
    return [cls(client, model=model, use_llm=use_llm) for cls in DEFAULT_ANALYSTS]
