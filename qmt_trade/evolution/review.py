"""盘后复盘归因（设计 L5）。

**这是相对 TradingAgents-CN 最重要的补齐之一**：它的 ``reflect_and_remember()``
全仓库唯一引用是 ``main.py`` 里的一行注释——进化闭环是死代码。

这里把复盘做成真正会跑、且**产出可被下游消费**的东西：

- 逐笔归因：每笔平仓拆解成「选股贡献 / 择时贡献 / 执行成本」三段；
- 因子有效性：入选标的的因子分位 vs 后续收益的秩相关（IC），喂给 optimizer；
- 经验条目：结构化 ``Lesson``，可入库、可检索、可回灌 prompt；
- 决策质量：Intent 的 conviction 与实际收益是否单调（不单调说明分档失效）。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class TradeAttribution:
    """单笔平仓的归因拆解。"""

    symbol: str
    opened_at: date | None
    closed_at: date | None
    holding_days: int
    entry: float
    exit: float
    shares: int
    gross_pnl: float          # 不含费用
    cost: float               # 佣金+税+过户+滑点
    net_pnl: float
    ret: float                # 净收益率（相对入场市值）
    reason: str = ""          # 平仓原因：STOP_LOSS / TIME_STOP / TRAILING / SIGNAL
    conviction: str = ""
    score: float = 0.0

    @property
    def win(self) -> bool:
        return self.net_pnl > 0

    @property
    def cost_drag(self) -> float:
        """费用对收益的拖累（占入场市值）。"""
        base = abs(self.entry * self.shares) or 1.0
        return self.cost / base


@dataclass
class Lesson:
    """结构化经验条目。可入库、可按 tag 检索、可回灌到 LLM prompt。"""

    asof: date
    tag: str                  # 如 "STOP_TOO_TIGHT" / "CONVICTION_INVERTED"
    severity: str             # INFO / WARN / CRITICAL
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""

    def render(self) -> str:
        return f"[{self.severity}] {self.tag}: {self.message}" + (
            f" → 建议: {self.suggestion}" if self.suggestion else "")


@dataclass
class ReviewResult:
    asof: date
    attributions: list[TradeAttribution] = field(default_factory=list)
    lessons: list[Lesson] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    factor_ic: dict[str, float] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.attributions)

    def report(self) -> str:
        s = self.stats
        lines = [
            "=" * 60,
            f"盘后复盘 asof={self.asof}  平仓笔数={self.n}",
            "=" * 60,
        ]
        if s:
            lines.append(
                f"  胜率={s.get('win_rate', 0):.1%}  盈亏比={s.get('profit_factor', 0):.2f}  "
                f"平均持有={s.get('avg_holding_days', 0):.1f}天  "
                f"净收益={s.get('net_pnl', 0):,.0f}  费用拖累={s.get('cost_drag', 0):.2%}")
            lines.append(
                f"  最大单笔盈={s.get('best', 0):,.0f}  最大单笔亏={s.get('worst', 0):,.0f}  "
                f"期望={s.get('expectancy', 0):,.1f}/笔")
        if self.factor_ic:
            top = sorted(self.factor_ic.items(), key=lambda kv: -abs(kv[1]))[:8]
            lines.append("  因子 IC: " + ", ".join(f"{k}={v:+.3f}" for k, v in top))
        by_reason = defaultdict(list)
        for a in self.attributions:
            by_reason[a.reason or "SIGNAL"].append(a.net_pnl)
        if by_reason:
            lines.append("  平仓原因: " + ", ".join(
                f"{k}×{len(v)}(净{sum(v):+,.0f})" for k, v in by_reason.items()))
        for les in self.lessons:
            lines.append("  " + les.render())
        return "\n".join(lines)


class ReviewEngine:
    """复盘引擎。输入 ``realized_log``（PortfolioState 维护）与可选的 Intent 记录。"""

    def __init__(self, settings=None):
        cfg = settings.section("evolution") if settings is not None else {}
        self.min_samples = int(cfg.get("review_min_samples", 5))
        self.cost_drag_warn = float(cfg.get("cost_drag_warn", 0.006))
        self.stop_hit_warn = float(cfg.get("stop_hit_warn", 0.45))

    # --------------------------------------------------------------- 主入口
    def run(self, asof: date, realized_log: list[dict], *,
            intents: dict[str, Any] | None = None,
            factor_frame=None, forward_returns: dict[str, float] | None = None
            ) -> ReviewResult:
        res = ReviewResult(asof=asof)
        res.attributions = self._attribute(realized_log, intents or {})
        res.stats = self._stats(res.attributions)
        if factor_frame is not None and forward_returns:
            res.factor_ic = self._factor_ic(factor_frame, forward_returns)
        res.lessons = self._lessons(asof, res)
        logger.info("复盘完成 asof=%s 平仓=%d 经验=%d", asof, res.n, len(res.lessons))
        return res

    # ---------------------------------------------------------------- 归因
    def _attribute(self, realized_log: list[dict],
                   intents: dict[str, Any]) -> list[TradeAttribution]:
        out: list[TradeAttribution] = []
        for r in realized_log or []:
            sym = r.get("symbol", "")
            entry = float(r.get("entry_price") or r.get("avg_cost") or 0.0)
            exit_ = float(r.get("exit_price") or r.get("price") or 0.0)
            shares = int(r.get("shares") or r.get("quantity") or 0)
            cost = float(r.get("cost") or 0.0)
            gross = (exit_ - entry) * shares
            net = float(r.get("pnl", gross - cost))
            base = abs(entry * shares) or 1.0
            opened = r.get("opened_at")
            closed = r.get("closed_at") or r.get("date")
            hd = int(r.get("holding_days") or (
                (closed - opened).days if isinstance(opened, date) and isinstance(closed, date) else 0))
            it = intents.get(sym)
            out.append(TradeAttribution(
                symbol=sym, opened_at=opened if isinstance(opened, date) else None,
                closed_at=closed if isinstance(closed, date) else None,
                holding_days=hd, entry=entry, exit=exit_, shares=shares,
                gross_pnl=gross, cost=cost, net_pnl=net, ret=net / base,
                reason=str(r.get("reason") or ""),
                conviction=getattr(it, "conviction", "") if it else str(r.get("conviction") or ""),
                score=float(getattr(it, "confidence", 0.0) if it else r.get("score") or 0.0),
            ))
        return out

    # ---------------------------------------------------------------- 统计
    def _stats(self, atts: list[TradeAttribution]) -> dict[str, Any]:
        if not atts:
            return {}
        wins = [a.net_pnl for a in atts if a.win]
        losses = [a.net_pnl for a in atts if not a.win]
        gross_win, gross_loss = sum(wins), abs(sum(losses))
        net = sum(a.net_pnl for a in atts)
        base = sum(abs(a.entry * a.shares) for a in atts) or 1.0
        return {
            "n": len(atts),
            "win_rate": len(wins) / len(atts),
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "net_pnl": net,
            "expectancy": net / len(atts),
            "avg_holding_days": float(np.mean([a.holding_days for a in atts])),
            "cost_drag": sum(a.cost for a in atts) / base,
            "best": max((a.net_pnl for a in atts), default=0.0),
            "worst": min((a.net_pnl for a in atts), default=0.0),
            "avg_win": float(np.mean(wins)) if wins else 0.0,
            "avg_loss": float(np.mean(losses)) if losses else 0.0,
        }

    # ------------------------------------------------------------ 因子 IC
    def _factor_ic(self, frame, forward_returns: dict[str, float]) -> dict[str, float]:
        """Spearman IC：因子分位 vs 后续收益的秩相关。喂给 optimizer 调权。"""
        import pandas as pd
        if frame is None or getattr(frame, "empty", True):
            return {}
        df = frame.set_index("symbol", drop=False)
        fwd = pd.Series(forward_returns, dtype=float)
        fwd = fwd[fwd.index.isin(df.index)]
        if len(fwd) < self.min_samples:
            return {}
        out: dict[str, float] = {}
        for col in df.columns:
            if not (col.endswith("_q") or col.startswith("cat_") or col == "score"):
                continue
            s = pd.to_numeric(df.loc[fwd.index, col], errors="coerce")
            if s.notna().sum() < self.min_samples or s.nunique() < 3:
                continue
            ic = s.corr(fwd, method="spearman")
            if ic is not None and not np.isnan(ic):
                out[col] = round(float(ic), 4)
        return out

    # ---------------------------------------------------------------- 经验
    def _lessons(self, asof: date, res: ReviewResult) -> list[Lesson]:
        out: list[Lesson] = []
        atts, s = res.attributions, res.stats

        # 因子 IC 是**截面统计**，样本量来自当日入选标的数，与平仓笔数无关，
        # 因此不受下面的"平仓样本不足"闸门约束——否则刚上线、还没平仓的阶段
        # 永远发现不了因子方向反了。
        for k, v in res.factor_ic.items():
            if v <= -0.15:
                out.append(Lesson(
                    asof, "FACTOR_INVERTED", "WARN",
                    f"因子 {k} 的 IC={v:+.3f} 显著为负，方向可能反了",
                    {"factor": k, "ic": v}, "下轮寻优中降权或反向验证"))

        if len(atts) < self.min_samples:
            out.append(Lesson(asof, "SAMPLE_TOO_SMALL", "INFO",
                              f"平仓样本仅 {len(atts)} 笔，结论不具统计意义",
                              {"n": len(atts)}, "累积 ≥30 笔后再据此调参"))
            return out

        # 1) 费用拖累过高 → 换手太快或滑点模型偏乐观
        if s.get("cost_drag", 0) > self.cost_drag_warn:
            out.append(Lesson(
                asof, "COST_DRAG_HIGH", "WARN",
                f"费用拖累 {s['cost_drag']:.2%} 超过警戒 {self.cost_drag_warn:.2%}",
                {"cost_drag": s["cost_drag"], "avg_holding": s["avg_holding_days"]},
                "延长持有期或提高入选门槛，降低换手"))

        # 2) 止损触发占比过高 → 止损过紧（被噪声扫出）
        stops = [a for a in atts if a.reason == "STOP_LOSS"]
        if atts and len(stops) / len(atts) > self.stop_hit_warn:
            out.append(Lesson(
                asof, "STOP_TOO_TIGHT", "WARN",
                f"止损触发占比 {len(stops) / len(atts):.0%}，可能被日内噪声扫出",
                {"stop_ratio": len(stops) / len(atts)},
                "提高 ATR 止损倍数，或改用结构位止损"))

        # 3) conviction 分档失效 → HIGH 的平均收益不高于 LOW
        by_conv: dict[str, list[float]] = defaultdict(list)
        for a in atts:
            if a.conviction:
                by_conv[a.conviction].append(a.ret)
        if {"HIGH", "LOW"} <= set(by_conv):
            hi, lo = float(np.mean(by_conv["HIGH"])), float(np.mean(by_conv["LOW"]))
            if hi <= lo:
                out.append(Lesson(
                    asof, "CONVICTION_INVERTED", "CRITICAL",
                    f"HIGH 档平均收益 {hi:.2%} 不高于 LOW 档 {lo:.2%}，分档失效",
                    {"high": hi, "low": lo},
                    "检查 conviction 生成逻辑；短期内可将仓位分档拉平"))

        # 4) 盈亏比过低 → 止盈过早
        pf = s.get("profit_factor", 0)
        if pf < 1.0 and s.get("win_rate", 0) >= 0.5:
            out.append(Lesson(
                asof, "CUT_WINNERS_EARLY", "WARN",
                f"胜率 {s['win_rate']:.0%} 但盈亏比仅 {pf:.2f}，盈利单被过早了结",
                {"profit_factor": pf, "avg_win": s.get("avg_win"),
                 "avg_loss": s.get("avg_loss")},
                "拉高第一档止盈位，或引入移动止盈让利润奔跑"))
        return out
