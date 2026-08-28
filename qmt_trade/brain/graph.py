"""智能体编排（设计 6.4.2）。

ADR-6 原写「编排用 LangGraph」，实施时改为**自研轻量状态机**，理由：

- LangGraph 引入较重依赖与版本耦合，而我们的拓扑是固定的 DAG（无循环，辩论轮次
  在节点内部处理），用不上它的检查点/流式能力；
- 我们需要「每个节点都能无 LLM 降级」，自研更可控；
- 更重要的是 P6（可复现）：自研能保证节点执行顺序与耗时统计完全确定。

拓扑：
    FactPack(非LLM) → 4 分析师(并行) → 事实校验(非LLM) → 多空辩论(动态轮)
    → 研究主管 → 组合经理 → 风险官 → TradeIntent → factcheck(非LLM)
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

import pandas as pd

from .agents.analysts import build_analysts
from .agents.debate import DebateModerator, ResearchManager, run_debate
from .agents.portfolio_manager import PortfolioManager
from .agents.risk_officer import RiskOfficer
from .factcheck import check_intent, verify_numbers
from .factpack import FactPackBuilder
from .schemas import TradeIntent
from .state import AgentState, PortfolioSnapshot

logger = logging.getLogger(__name__)


@dataclass
class FinalPick:
    """最终精选：多 Agent 投票后进入交易候选的 3~5 只高胜率标的。

    与 TradeIntent 的区别：Intent 是全部通过风控门槛的研判产出（≤10 个），
    精选是其中优中选优的子集，附带选中理由与投票汇总，直接面向交易与展示。
    """

    symbol: str
    action: str
    conviction: str
    confidence: float
    industry: str = ""
    factor_score: float = 0.0
    votes: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    #: 多空辩论（结构化，可用于前端"看多方辩论"展示）
    debate: list[dict] = field(default_factory=list)
    #: 看多方核心论据 / 看空方核心论据（由辩论提炼）
    bull_case: str = ""
    bear_case: str = ""
    #: 支撑证据：类别分位 + 关键因子原值（让理由可核查）
    evidence: list[dict] = field(default_factory=list)
    intent: TradeIntent | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "action": self.action,
            "conviction": self.conviction, "confidence": round(self.confidence, 3),
            "industry": self.industry, "factor_score": round(self.factor_score, 4),
            "votes": dict(self.votes), "reason": self.reason,
            "debate": list(self.debate), "bull_case": self.bull_case,
            "bear_case": self.bear_case, "evidence": list(self.evidence),
        }


@dataclass
class BrainResult:
    """一次批量研判的产出。"""

    asof: date
    intents: list[TradeIntent] = field(default_factory=list)
    picks: list[FinalPick] = field(default_factory=list)
    states: dict[str, AgentState] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)
    elapsed: float = 0.0
    llm_calls: int = 0
    llm_cached: int = 0
    llm_cost_cny: float = 0.0
    degraded: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.intents)

    def report(self) -> str:
        lines = [
            f"{'=' * 60}",
            f"LLM 研判 asof={self.asof}  产出 Intent={self.n}  精选={len(self.picks)}  "
            f"否决={len(self.rejected)}  耗时={self.elapsed:.2f}s",
            f"  LLM 实调={self.llm_calls} 缓存命中={self.llm_cached} "
            f"成本=¥{self.llm_cost_cny:.4f}"
            + ("  ⚠ 降级: " + ";".join(sorted(set(self.degraded))) if self.degraded else ""),
            f"{'=' * 60}",
        ]
        if self.picks:
            lines.append("  最终精选:")
            for i, pk in enumerate(self.picks, 1):
                lines.append(f"    #{i} {pk.symbol} {pk.action} conv={pk.conviction} "
                             f"conf={pk.confidence:.2f} 理由: {pk.reason[:80]}")
        for it in self.intents[:10]:
            lines.append(
                f"  {it.symbol}  {it.action}  conv={it.conviction:<6} "
                f"conf={it.confidence:.2f} stop={it.stop_loss_value:.1%} "
                f"budget={it.risk_budget_hint:.2f} flags={','.join(it.risk_flags) or '-'}"
            )
        if self.rejected:
            items = list(self.rejected.items())[:8]
            lines.append("  否决: " + "; ".join(f"{k}({v})" for k, v in items))
        return "\n".join(lines)


# 证据展示用的标签映射
_CAT_EVIDENCE_KEYS = [
    ("动量类分位", "动量"), ("资金类分位", "资金流"), ("基本面类分位", "基本面"),
    ("情绪类分位", "市场情绪"), ("质量类分位", "质量"),
]
_RAW_EVIDENCE_KEYS = [
    "20日涨跌幅", "60日涨跌幅", "ROE", "净利同比", "营收同比",
    "近5日主力净流入占成交比", "距60日高点比", "偿债安全分",
    "近5日新闻情绪", "ATR占价比", "60日最大回撤",
]


def _evidence_from_state(state: "AgentState") -> list[dict]:
    """从事实卡片提炼支撑证据：类别分位（核心打分驱动）+ 关键因子原值。

    返回 [{label, key, value, kind, verdict}]，verdict ∈ bull/bear/neutral，
    供前端按颜色高亮。pct 类 value∈[0,1]，raw 类为真实数值。
    """
    fp = state.factpack.numerics
    ev: list[dict] = []
    for key, label in _CAT_EVIDENCE_KEYS:
        v = fp.get(key)
        if isinstance(v, (int, float)):
            verdict = "bull" if v >= 0.6 else ("bear" if v <= 0.4 else "neutral")
            ev.append({"label": label, "key": key, "value": round(v, 3),
                       "kind": "pct", "verdict": verdict})
    for key in _RAW_EVIDENCE_KEYS:
        v = fp.get(key)
        if isinstance(v, (int, float)):
            ev.append({"label": key, "key": key, "value": round(v, 4),
                       "kind": "raw", "verdict": "neutral"})
    return ev[:12]


class BrainGraph:
    """多智能体研判图。

    Parameters
    ----------
    client : LLMClient | None
        为 None 或 ``use_llm=False`` 时全程走规则路径（P5 纯因子模式）。
    max_workers : int
        分析师并行度。设计 6.4.1：串行 → 并行，延迟降约 60%。
    """

    def __init__(self, settings=None, hub=None, client=None, *, use_llm: bool = False,
                 max_workers: int = 4, min_intent_score: float = 0.50,
                 max_holding_days: int = 20):
        self.settings = settings
        self.hub = hub
        self.client = client
        self.use_llm = bool(use_llm and client is not None)
        self.max_workers = max(1, max_workers)
        self.min_intent_score = min_intent_score

        cfg = settings.section("brain") if settings is not None else {}
        self.min_intent_score = float(cfg.get("min_intent_score", min_intent_score))
        self.max_intents = int(cfg.get("max_intents", 10))
        self.picks_min = int(cfg.get("final_picks_min", 3))
        self.picks_max = int(cfg.get("final_picks_max", 5))
        # 候选瘦身：只对排名前 K 的标的做全量多 Agent LLM 研判，其余走规则快筛。
        # 最终只产出 ≤max_intents 个 Intent / ≤picks_max 只精选，
        # 给排名靠后的候选烧 LLM 属于纯浪费（30 只全量 ≈300 次调用 → K=12 后 ≈120 次）。
        self.llm_top_k = int(cfg.get("llm_top_k", 12))
        debate_cfg = cfg.get("debate", {}) if isinstance(cfg, dict) else {}

        self.builder = FactPackBuilder(hub)
        self.analysts = build_analysts(client, use_llm=self.use_llm)
        self.moderator = DebateModerator(
            min_rounds=int(debate_cfg.get("min_rounds", 1)),
            max_rounds=int(debate_cfg.get("max_rounds", 3)),
        )
        self.research_manager = ResearchManager(client, use_llm=self.use_llm)
        self.portfolio_manager = PortfolioManager(
            client, use_llm=self.use_llm,
            max_industry_weight=float(cfg.get("max_industry_weight", 0.30)),
        )
        self.risk_officer = RiskOfficer(
            client, use_llm=self.use_llm, max_holding_days=max_holding_days)

    # ------------------------------------------------------------- 单标的
    def run_one(self, symbol: str, row: pd.Series | dict, asof: date,
                snapshot: PortfolioSnapshot, *, regime: str = "RANGE",
                lessons: Sequence[str] = (),
                use_llm: bool | None = None) -> AgentState:
        """单标的研判。``use_llm=None`` 跟随全图配置；显式传 False 走规则快筛
        （候选瘦身用：排名靠后的标的不值得烧 LLM）。"""
        eff_llm = self.use_llm if use_llm is None else bool(use_llm and self.client is not None)
        row = dict(row) if not isinstance(row, dict) else row
        fp = self.builder.build(symbol, row, asof)
        state = AgentState(
            symbol=symbol, asof=asof, factpack=fp, portfolio=snapshot, regime=regime,
            rank=int(row.get("rank") or 999), score=float(row.get("score") or 0.5),
            ref_price=float(row.get("close") or 0.0), industry=str(row.get("industry") or ""),
            lessons=list(lessons),
        )

        # 临时切换共享 Agent 实例的 LLM 开关（run() 内逐标的串行执行，无并发冲突）
        llm_agents = (*self.analysts, self.research_manager,
                      self.portfolio_manager, self.risk_officer)
        saved = [(a, a.use_llm) for a in llm_agents]
        for a in llm_agents:
            a.use_llm = eff_llm
        try:
            # ── 1. 分析师并行 fan-out ─────────────────────────────────────────
            with state.timeit("analysts"):
                if self.max_workers > 1 and len(self.analysts) > 1:
                    with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                        for rep in pool.map(lambda a: a.run(state), self.analysts):
                            state.reports[rep.agent] = rep
                else:
                    for a in self.analysts:
                        rep = a.run(state)
                        state.reports[rep.agent] = rep

            # ── 2. 事实校验（非 LLM）：LLM 引用的数字必须能在 FactPack 中找到 ──
            with state.timeit("verify"):
                for name, rep in state.reports.items():
                    if not rep.cited_numbers:
                        continue
                    vr = verify_numbers(rep.cited_numbers, fp)
                    if not vr.ok:
                        rep.ok = False
                        rep.issues.extend(vr.issues[:3])
                        rep.confidence *= 0.5          # 疑似幻觉 → 信心腰斩
                        state.degraded.append(f"{name}:数字未对上")

            if state.veto_reason:                      # 硬负面事件等：规则先行，直接短路
                return state

            # ── 3. 多空辩论（结构化，动态轮次） ──────────────────────────────
            run_debate(state, client=self.client, use_llm=eff_llm, moderator=self.moderator)

            # ── 4. 研究主管 → 5. 组合经理 → 6. 风险官 ───────────────────────
            for agent in (self.research_manager, self.portfolio_manager, self.risk_officer):
                rep = agent.run(state)
                state.reports[rep.agent] = rep
                if state.veto_reason:
                    return state
            return state
        finally:
            for a, v in saved:
                a.use_llm = v

    # --------------------------------------------------------------- 批量
    def run(self, candidates, portfolio_snapshot: PortfolioSnapshot | None = None,
            *, symbols: list[str] | None = None,
            lessons: Sequence[str] = ()) -> BrainResult:
        """对 CandidateSet 的 shortlist 批量研判，产出 TradeIntent 列表。

        :param lessons: L5 复盘沉淀的近期经验教训，注入分析师 prompt，
                        让“每日复盘 → 优化研判”闭环真正闭合。
        """
        t0 = time.perf_counter()
        asof = candidates.asof
        regime = getattr(candidates.regime, "regime", None)
        regime_name = getattr(regime, "value", str(regime or "RANGE"))
        snap = portfolio_snapshot or PortfolioSnapshot(
            total_asset=1.0, cash=1.0,
            max_position_pct=getattr(candidates.regime, "max_position", 0.8))

        res = BrainResult(asof=asof)
        if candidates.is_empty:
            res.elapsed = time.perf_counter() - t0
            return res

        syms = symbols if symbols is not None else candidates.shortlist(self.max_intents * 3)
        frame = candidates.frame.set_index("symbol", drop=False)

        for idx, s in enumerate(syms):
            if s not in frame.index:
                continue
            row = frame.loc[s]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            try:
                # 候选瘦身：syms 为 rank 升序，只有前 llm_top_k 只走全量 LLM 多 Agent，
                # 其余规则快筛（零成本，仍可凭因子分通过风控门槛，不损失覆盖面）
                state = self.run_one(s, row, asof, snap, regime=regime_name,
                                     lessons=lessons,
                                     use_llm=None if not self.use_llm else idx < self.llm_top_k)
            except Exception as exc:                # 单票失败不拖垮整批
                logger.warning("研判 %s 失败: %s", s, exc)
                res.rejected[s] = f"异常:{type(exc).__name__}"
                continue

            res.states[s] = state
            res.llm_calls += state.llm_calls
            res.llm_cached += state.llm_cached
            res.llm_cost_cny += state.llm_cost_cny
            res.degraded.extend(state.degraded)

            intent = self.risk_officer.make_intent(state, min_score=self.min_intent_score)
            if intent is None:
                res.rejected[s] = state.veto_reason or "未达研判门槛"
                continue
            fc = check_intent(intent, asof)
            if not fc.ok:                            # P3：不合法的 Intent 一律丢弃
                res.rejected[s] = "factcheck:" + ";".join(fc.issues[:2])
                continue
            res.intents.append(intent)

        # 按置信度降序，截断到 max_intents（设计：每日 ≤10 个 Intent）
        res.intents.sort(key=lambda i: (-i.confidence, i.symbol))
        if len(res.intents) > self.max_intents:
            for it in res.intents[self.max_intents:]:
                res.rejected[it.symbol] = "超出每日 Intent 上限"
            res.intents = res.intents[: self.max_intents]

        # 优中选优：从 Intent 里挑出 3~5 只最终精选（行业分散 + 选中理由）
        res.picks = self._final_picks(res)

        res.elapsed = time.perf_counter() - t0
        logger.info("LLM 研判完成 asof=%s 候选=%d(LLM前%d) → Intent=%d 精选=%d 否决=%d "
                    "耗时=%.2fs LLM实调=%d 缓存命中=%d 成本=¥%.4f",
                    asof, len(syms), self.llm_top_k if self.use_llm else 0,
                    res.n, len(res.picks), len(res.rejected), res.elapsed,
                    res.llm_calls, res.llm_cached, res.llm_cost_cny)
        return res

    # ------------------------------------------------------------- 最终精选
    def _final_picks(self, res: BrainResult) -> list[FinalPick]:
        """从通过门槛的 Intent 中选出最终精选。

        规则：只取开仓类（BUY/ADD）；按置信度降序；单行业不超过一半名额
        （行业分散是胜率的一部分）；达不到 picks_min 宁缺毋滥。
        """
        buys = [it for it in res.intents if it.action in ("BUY", "ADD")]
        out: list[FinalPick] = []
        per_ind: dict[str, int] = {}
        ind_cap = max(1, self.picks_max // 2)
        for it in buys:                              # intents 已按置信度降序
            if len(out) >= self.picks_max:
                break
            st = res.states.get(it.symbol)
            ind = getattr(st, "industry", "") or ""
            if per_ind.get(ind, 0) >= ind_cap and len(out) < self.picks_max:
                continue
            out.append(self._build_pick(it, st))
            per_ind[ind] = per_ind.get(ind, 0) + 1
        return out

    @staticmethod
    def _build_pick(intent: TradeIntent, state: AgentState | None) -> FinalPick:
        """汇总选中理由：研判结论 > 投资论点 > 分析师要点，附各 Agent 投票。

        新增：把多空辩论、看多/看空核心论据、支撑证据（因子分位+关键原值）
        一并带出，让前端能展示"有凭有据"的精选，而不是一句空话。
        """
        votes = dict(intent.agent_votes or (state.votes if state else {}))
        reason = (intent.reasoning or "").strip()
        if not reason and state is not None:
            reason = (state.thesis or "").strip()
        if not reason and state is not None:
            hi: list[str] = []
            for rep in state.reports.values():
                hi.extend(rep.highlights[:1])
            reason = "; ".join(hi[:3])
        if not reason:
            reason = f"因子打分通过风控门槛，置信度 {intent.confidence:.2f}"

        debate: list[dict] = []
        bull_case = ""
        bear_case = ""
        evidence: list[dict] = []
        if state is not None:
            debate = [
                {"round": t.round_no, "stance": t.stance, "speaker": t.speaker,
                 "claim": t.claim, "confidence": round(t.confidence, 3)}
                for t in state.debate
            ]
            bull_case = state.bull_case or ""
            bear_case = state.bear_case or ""
            evidence = _evidence_from_state(state)
        return FinalPick(
            symbol=intent.symbol, action=intent.action,
            conviction=intent.conviction, confidence=intent.confidence,
            industry=getattr(state, "industry", "") or "",
            factor_score=float(getattr(state, "score", 0.0) or 0.0),
            votes=votes, reason=reason, debate=debate,
            bull_case=bull_case, bear_case=bear_case, evidence=evidence,
            intent=intent,
        )


def build_brain(settings, hub=None, *, use_llm: bool | None = None,
                llm_config_path=None, **kwargs) -> BrainGraph:
    """装配 BrainGraph。

    LLM 配置已从 ``settings.yaml`` 剥离到独立的 ``config/llm.yaml``：
    ``LLMManager.from_file()`` 读取多平台/多模型与场景路由；``enabled=false``
    或装配失败时自动降级纯因子模式（P5）。
    """
    from .llm.manager import LLMManager
    client = None
    if use_llm is not False:
        try:
            mgr = LLMManager.from_file(llm_config_path)
            if mgr.enabled:
                client = mgr
        except Exception as exc:
            logger.warning("LLM 管理层装配失败，降级纯因子模式: %s", exc)
    enabled = bool(client is not None) if use_llm is None else bool(use_llm and client is not None)
    return BrainGraph(settings, hub, client, use_llm=enabled, **kwargs)
