"""多空辩论与研究主管（设计 6.4.2）。

三处关键改造：

1. **结构化辩论**：``list[DebateTurn]`` 而非字符串拼接，token 不随轮次线性膨胀；
2. **动态轮次**：分析师一致性高 → 1 轮就收；分歧大/置信度低 → 最多 3 轮；
3. **无 LLM 也能辩**：规则路径下，多空双方各自摆出对方维度的因子证据，
   由研究主管按加权分裁决——这保证 P5 下辩论层不是摆设。
"""

from __future__ import annotations

from ..state import AgentState, AnalystReport, DebateTurn
from .base import Agent, parse_json_loose, stance_of

#: 类别分位 → 中文标签（用于把"看多/看空证据"结构化呈现）
_CAT_PCT_LABELS = [
    ("动量类分位", "动量"),
    ("资金类分位", "资金流"),
    ("基本面类分位", "基本面"),
    ("情绪类分位", "市场情绪"),
    ("质量类分位", "质量"),
]


def _category_signals(state: AgentState):
    """从事实卡片的类别分位提炼看多/看空证据（≥0.6 强，≤0.4 弱）。

    这是"理由牵强"问题的根因修复点：过去论点只说"因子入选"，
    现在必须点名具体因子分位，让理由可核查、可归因。
    """
    fp = state.factpack.numerics
    bull, bear = [], []
    for key, label in _CAT_PCT_LABELS:
        v = fp.get(key)
        if not isinstance(v, (int, float)):
            continue
        if v >= 0.6:
            bull.append(f"{label}分位 {v:.2f}（强）")
        elif v <= 0.4:
            bear.append(f"{label}分位 {v:.2f}（弱）")
    return bull, bear


def _debate_cases(state: AgentState, limit: int = 600):
    """把多空辩论发言压缩成多方/空方核心论据（结构化透传给前端）。"""
    bull = [t for t in state.debate if t.stance == "BULL"]
    bear = [t for t in state.debate if t.stance == "BEAR"]
    bc = "；".join(t.claim for t in bull)
    kc = "；".join(t.claim for t in bear)
    return (bc[:limit] if bc else ""), (kc[:limit] if kc else "")


class DebateModerator:
    """辩论主持：决定开几轮、何时提前结束。"""

    def __init__(self, *, min_rounds: int = 1, max_rounds: int = 3,
                 agreement_stop: float = 0.75, confidence_stop: float = 0.65):
        self.min_rounds = max(1, min_rounds)
        self.max_rounds = max(self.min_rounds, max_rounds)
        self.agreement_stop = agreement_stop
        self.confidence_stop = confidence_stop

    def rounds_needed(self, state: AgentState) -> int:
        """低一致性自动加轮，高一致性提前结束。"""
        agree = state.agreement
        conf = (sum(r.confidence for r in state.reports.values())
                / max(1, len(state.reports)))
        if agree >= self.agreement_stop and conf >= self.confidence_stop:
            return self.min_rounds
        if agree <= 0.5:
            return self.max_rounds
        return min(self.max_rounds, self.min_rounds + 1)


class _Debater(Agent):
    scene = "debate"                    # 多空辩论场景
    side: str = "BULL"

    def _rule_based(self, state: AgentState) -> AnalystReport:  # 未直接使用
        return AnalystReport(agent=self.name, stance=self.side)  # type: ignore[arg-type]

    def speak(self, state: AgentState, round_no: int) -> DebateTurn:
        """产出一轮发言。规则路径：把最支持己方的证据列出来。"""
        want_high = self.side == "BULL"
        picked: list[tuple[str, float]] = []
        for k, v in state.factpack.numerics.items():
            if not k.endswith("分位"):
                continue
            if (want_high and v >= 0.6) or ((not want_high) and v <= 0.4):
                picked.append((k, v))
        picked.sort(key=lambda kv: -kv[1] if want_high else kv[1])
        picked = picked[:3]

        if picked:
            claim = "；".join(f"{k}={v:.2f}" for k, v in picked)
            claim = (f"支持做多：{claim}" if want_high else f"提示风险：{claim}")
            conf = min(0.9, 0.5 + 0.1 * len(picked))
        else:
            claim = ("缺乏明确的多头证据" if want_high else "未发现显著的空头证据")
            conf = 0.35

        # 引用对手上一轮观点，形成真正的"辩论"而不是各说各话
        prev = [t for t in state.debate if t.stance != self.side]
        if prev:
            claim += f"｜回应对方：{prev[-1].claim[:60]}"

        turn = DebateTurn(round_no=round_no, stance=self.side,  # type: ignore[arg-type]
                          speaker=self.name, claim=claim,
                          evidence_keys=[k for k, _ in picked], confidence=conf)

        if self.use_llm:
            prompt = (
                f"SYMBOL: {state.symbol}\n"
                f"你是{'多头' if want_high else '空头'}辩手，第 {round_no} 轮。"
                f"只依据事实卡片，简洁给出最有力的一条论据，并指出对方观点的漏洞。\n"
                f"--- 事实卡片 ---\n{state.factpack.render()}\n"
                f"--- 已有辩论 ---\n{state.render_debate()}\n"
                f'严格输出 JSON: {{"claim": "一句话论据", "confidence": 0~1}}\n'
            )
            try:
                resp = self.client.complete(prompt, scene=self.scene, model=self.model,
                                            temperature=self.temperature, tag=self.name)
                if resp.cached:
                    state.llm_cached += 1
                else:
                    state.llm_calls += 1
                    state.llm_cost_cny += resp.cost_cny
                data = parse_json_loose(resp.content)
                if data.get("claim"):
                    turn.claim = str(data["claim"])[:400]
                if "confidence" in data:
                    try:
                        turn.confidence = max(0.0, min(1.0, float(data["confidence"])))
                    except (TypeError, ValueError):
                        pass
            except Exception as exc:
                state.errors.append(f"{self.name}:{type(exc).__name__}")
        return turn


class BullDebater(_Debater):
    name = "bull"
    side = "BULL"


class BearDebater(_Debater):
    name = "bear"
    side = "BEAR"


class ResearchManager(Agent):
    """研究主管：汇总分析师 + 辩论，形成投资论点与最终多空判断。"""

    scene = "market_analysis"          # 综合研判场景
    name = "research_manager"

    def _rule_based(self, state: AgentState) -> AnalystReport:
        bull = state.bull_score
        # 辩论调整：双方信心差决定微调幅度（上限 ±0.08，不能盖过因子体系）
        b = sum(t.confidence for t in state.debate if t.stance == "BULL")
        s = sum(t.confidence for t in state.debate if t.stance == "BEAR")
        adj = 0.0
        if b + s > 0:
            adj = max(-0.08, min(0.08, 0.16 * (b - s) / (b + s)))
        score = max(0.0, min(1.0, bull + adj))

        cat_bull, cat_bear = _category_signals(state)
        bull_case, bear_case = _debate_cases(state)
        state.bull_case = bull_case
        state.bear_case = bear_case

        # 证据化论点：必须点名具体因子分位/数值，不再说空话
        pct = state.factpack.numerics.get("综合分市场分位")
        pct_txt = f"（全市场分位 {pct:.2f}）" if isinstance(pct, (int, float)) else ""
        thesis = (
            f"综合分 {state.score:.3f}{pct_txt}，排名 {state.rank}。"
            f"看多证据：{ '；'.join(cat_bull) or '暂无显著看多因子' }。"
            f"风险信号：{ '；'.join(cat_bear) or '暂无显著风险因子' }。"
        )
        if bull_case:
            thesis += f" 多方：{bull_case[:140]}"
        if bear_case:
            thesis += f" 空方：{bear_case[:140]}"

        risks: list[str] = []
        for r in state.reports.values():
            risks.extend(r.risks[:2])
        highlights: list[str] = []
        for r in state.reports.values():
            if r.stance == "BULL":
                highlights.extend(r.highlights[:2])

        conf = state.agreement * 0.5 + min(1.0, state.factpack.coverage) * 0.5
        state.thesis = thesis
        return AnalystReport(
            agent=self.name, stance=stance_of(score), score=round(score, 4),
            confidence=round(conf, 3),
            highlights=(cat_bull + highlights)[:5], risks=(cat_bear + risks)[:5],
        )

    def _prompt(self, state: AgentState) -> str:
        if not self.use_llm:
            return ""
        votes = "\n".join(f"  {r.render()}" for r in state.reports.values())
        return (
            f"SYMBOL: {state.symbol}\n"
            f"你是研究主管。综合四位分析师结论与多空辩论，形成一段投资论点。\n"
            f"注意：不得引用事实卡片以外的数据。若证据不足，明确说明。\n"
            f"--- 分析师结论 ---\n{votes}\n"
            f"--- 辩论记录 ---\n{state.render_debate()}\n"
            f"--- 事实卡片 ---\n{state.factpack.render()}\n"
            f'严格输出 JSON: {{"score": 0~1, "confidence": 0~1, '
            f'"thesis": "投资论点(<=150字，必须引用具体因子分位/数值)", '
            f'"bull_case": "多方核心论据(必须引用事实卡片中的具体证据)", '
            f'"bear_case": "空方核心论据(必须引用事实卡片中的具体证据)", '
            f'"risks": ["风险1","风险2"]}}\n'
        )

    def _merge_llm(self, base, data, raw, state):
        base = super()._merge_llm(base, data, raw, state)
        if data.get("thesis"):
            state.thesis = str(data["thesis"])[:600]
        if data.get("bull_case"):
            state.bull_case = str(data["bull_case"])[:600]
        if data.get("bear_case"):
            state.bear_case = str(data["bear_case"])[:600]
        return base

    def run(self, state: AgentState) -> AnalystReport:
        rep = super().run(state)
        if not state.thesis:
            state.thesis = (
                f"综合分 {state.score:.3f}（排名 {state.rank}），"
                f"多头分 {rep.score:.2f}，一致性 {state.agreement:.0%}。"
                f"要点：{'; '.join(rep.highlights[:3]) or '因子入选'}。"
                f"风险：{'; '.join(rep.risks[:2]) or '常规市场风险'}。"
            )
        return rep


def run_debate(state: AgentState, *, client=None, use_llm: bool = False,
               moderator: DebateModerator | None = None) -> None:
    """执行动态轮次的多空辩论，结果写入 ``state.debate``。"""
    mod = moderator or DebateModerator()
    bull, bear = BullDebater(client, use_llm=use_llm), BearDebater(client, use_llm=use_llm)
    n = mod.rounds_needed(state)
    with state.timeit("debate"):
        for i in range(1, n + 1):
            state.debate.append(bull.speak(state, i))
            state.debate.append(bear.speak(state, i))
            # 提前收敛：本轮双方信心都不高，再辩也无意义
            if i >= mod.min_rounds and abs(
                state.debate[-1].confidence - state.debate[-2].confidence
            ) < 0.1 and state.agreement >= mod.agreement_stop:
                break
