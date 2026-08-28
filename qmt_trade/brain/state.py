"""智能体运行状态（设计 6.4.2）。

对 TradingAgents-CN 的三处改造落在这里：

1. **辩论历史结构化**：``list[DebateTurn]`` 取代字符串拼接，token 可裁剪、可检索、可归因；
2. **组合视角**：``AgentState`` 不再是 ``company_of_interest: str``，而是携带
   ``PortfolioSnapshot``（现有持仓 / 可用资金 / 行业暴露 / 当日已开仓数）；
3. **节点耗时与投票留痕**：``node_timings`` / ``votes``，用于事后归因与性能分析。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

Stance = Literal["BULL", "BEAR", "NEUTRAL"]


# --------------------------------------------------------------------- FactPack
@dataclass
class Fact:
    """一条事实。``asof`` 是这条数据本身的日期，用于防幻觉与 PIT 追溯。"""

    key: str
    value: Any
    asof: date | None = None
    source: str = ""
    unit: str = ""

    def render(self) -> str:
        v = self.value
        if isinstance(v, float):
            v = f"{v:.4g}"
        tail = f"（截至 {self.asof}）" if self.asof else ""
        unit = self.unit or ""
        src = f" [{self.source}]" if self.source else ""
        return f"- {self.key}: {v}{unit}{tail}{src}"


@dataclass
class FactPack:
    """喂给 LLM 的"事实卡片"。非 LLM 生成，全部来自结构化数据（防幻觉的第一道闸）。"""

    symbol: str
    asof: date
    industry: str = ""
    name: str = ""
    facts: list[Fact] = field(default_factory=list)
    #: 数值索引：key -> float，供事实校验器比对 LLM 输出中的数字
    numerics: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    def add(self, key: str, value: Any, *, asof: date | None = None,
            source: str = "", unit: str = "") -> None:
        if value is None:
            self.missing.append(key)
            return
        self.facts.append(Fact(key, value, asof or self.asof, source, unit))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.numerics[key] = float(value)

    def render(self) -> str:
        head = f"标的: {self.symbol} {self.name}  行业: {self.industry or '未知'}  数据基准日: {self.asof}"
        body = "\n".join(f.render() for f in self.facts)
        tail = f"\n- 缺失字段（不得臆测）: {', '.join(self.missing)}" if self.missing else ""
        return f"{head}\n{body}{tail}"

    @property
    def coverage(self) -> float:
        total = len(self.facts) + len(self.missing)
        return len(self.facts) / total if total else 0.0


# ----------------------------------------------------------------------- 辩论
@dataclass
class DebateTurn:
    """一轮辩论中的一次发言。结构化而非字符串拼接。"""

    round_no: int
    stance: Stance
    speaker: str
    claim: str
    evidence_keys: list[str] = field(default_factory=list)
    confidence: float = 0.5

    def render(self, max_chars: int = 400) -> str:
        c = self.claim if len(self.claim) <= max_chars else self.claim[:max_chars] + "…"
        return f"[R{self.round_no}/{self.stance}/{self.speaker} conf={self.confidence:.2f}] {c}"


@dataclass
class AnalystReport:
    """单个分析师的结构化产出。不再用 ``len(report)>100`` 判完成（设计 6.4.1）。"""

    agent: str
    stance: Stance
    score: float = 0.5              # 0~1，该维度的看多程度
    confidence: float = 0.5
    highlights: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    cited_numbers: list[float] = field(default_factory=list)
    raw: str = ""
    ok: bool = True
    issues: list[str] = field(default_factory=list)

    def render(self) -> str:
        h = "; ".join(self.highlights[:3]) or "无"
        r = "; ".join(self.risks[:3]) or "无"
        return (f"{self.agent}: 立场={self.stance} 分={self.score:.2f} "
                f"信心={self.confidence:.2f}\n  要点: {h}\n  风险: {r}")


# ------------------------------------------------------------------- 组合快照
@dataclass
class PortfolioSnapshot:
    """给组合经理看的账户上下文（TradingAgents-CN 完全缺失的一块）。"""

    total_asset: float = 0.0
    cash: float = 0.0
    position_weight: dict[str, float] = field(default_factory=dict)
    industry_weight: dict[str, float] = field(default_factory=dict)
    opened_today: int = 0
    max_positions: int = 10
    max_position_pct: float = 0.8
    drawdown: float = 0.0

    @property
    def n_positions(self) -> int:
        return len(self.position_weight)

    @property
    def gross_weight(self) -> float:
        return float(sum(self.position_weight.values()))

    def render(self) -> str:
        pos = ", ".join(f"{k}={v:.1%}" for k, v in sorted(
            self.position_weight.items(), key=lambda kv: -kv[1])[:8]) or "空仓"
        ind = ", ".join(f"{k}={v:.1%}" for k, v in sorted(
            self.industry_weight.items(), key=lambda kv: -kv[1])[:5]) or "无"
        return (f"总资产={self.total_asset:,.0f} 现金={self.cash:,.0f} "
                f"持仓数={self.n_positions}/{self.max_positions} "
                f"总仓位={self.gross_weight:.1%}/{self.max_position_pct:.0%} "
                f"回撤={self.drawdown:.2%} 今日已开={self.opened_today}\n"
                f"  持仓: {pos}\n  行业: {ind}")

    @classmethod
    def from_portfolio(cls, pf, sym_industry: dict[str, str] | None = None,
                       *, max_positions: int = 10, max_position_pct: float = 0.8,
                       opened_today: int = 0) -> "PortfolioSnapshot":
        sym_industry = sym_industry or {}
        total = pf.total_asset or 1.0
        pw = {s: (p.shares * p.avg_cost / total)
              for s, p in pf.positions.items() if p.shares > 0}
        iw: dict[str, float] = {}
        for s, w in pw.items():
            ind = sym_industry.get(s) or getattr(pf.positions[s], "industry", "") or "未知"
            iw[ind] = iw.get(ind, 0.0) + w
        # PortfolioState.max_drawdown 返回正数幅度，这里统一成"负数=亏损"的表达
        return cls(
            total_asset=pf.total_asset, cash=pf.cash, position_weight=pw,
            industry_weight=iw, opened_today=opened_today,
            max_positions=max_positions, max_position_pct=max_position_pct,
            drawdown=-abs(pf.max_drawdown),
        )


# ------------------------------------------------------------------ AgentState
@dataclass
class AgentState:
    """一次「单标的研判」的完整状态。图里每个节点读它、写它。"""

    symbol: str
    asof: date
    factpack: FactPack
    portfolio: PortfolioSnapshot
    regime: str = "RANGE"
    rank: int = 999
    score: float = 0.5
    ref_price: float = 0.0
    industry: str = ""

    reports: dict[str, AnalystReport] = field(default_factory=dict)
    debate: list[DebateTurn] = field(default_factory=list)
    thesis: str = ""
    #: 由多空辩论提炼的"看多方核心论据"与"看空方核心论据"（结构化、可展示）
    bull_case: str = ""
    bear_case: str = ""
    votes: dict[str, str] = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)
    #: L5 复盘回灌的经验教训（近期 WARN/CRITICAL Lesson），供分析师 prompt 引用
    lessons: list[str] = field(default_factory=list)

    node_timings: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    llm_calls: int = 0
    #: 缓存命中次数：只计数不产生新费用，与 llm_calls（实调）分开统计
    llm_cached: int = 0
    llm_cost_cny: float = 0.0

    #: 终止标记：非空表示中途被否决，不产生 Intent
    veto_reason: str = ""

    def timeit(self, node: str):
        return _Timer(self, node)

    @property
    def bull_score(self) -> float:
        """各分析师加权看多分（无报告时回退因子分）。"""
        if not self.reports:
            return float(self.score)
        tot_w = sum(r.confidence for r in self.reports.values()) or 1.0
        return sum(r.score * r.confidence for r in self.reports.values()) / tot_w

    @property
    def agreement(self) -> float:
        """分析师一致性：立场众数占比。低一致性 → 辩论加轮。"""
        if len(self.reports) < 2:
            return 1.0
        stances = [r.stance for r in self.reports.values()]
        top = max(set(stances), key=stances.count)
        return stances.count(top) / len(stances)

    def render_debate(self, max_turns: int = 8) -> str:
        turns = self.debate[-max_turns:]
        return "\n".join(t.render() for t in turns) or "（无辩论记录）"


class _Timer:
    def __init__(self, state: AgentState, node: str):
        self.state, self.node = state, node

    def __enter__(self):
        self._t = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.state.node_timings[self.node] = time.perf_counter() - self._t
        return False
