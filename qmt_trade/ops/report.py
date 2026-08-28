"""日报 / 周报（设计 L6）。

报告的读者只有一个人：**明早的你**。所以它必须在 30 秒内回答三个问题：

1. 昨天赚了还是亏了，钱去哪了（费用/滑点吃了多少）；
2. 系统有没有带病运行（风控事件、被拒订单、体检项）；
3. 有没有需要人工介入的事（对账不平、KillSwitch 挂起、复盘给出的经验）。

刻意**不做花哨图表**：M0-M3 阶段是 CLI + 消息推送，Markdown 纯文本在企微里能直接读。
数据全部来自 SQLite（P6：所有决策都落库），报告本身是纯函数——同样的库产出同样的报告。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ..core.logging import get_logger

logger = get_logger("ops.report")


def _pct(x: float | None, digits: int = 2) -> str:
    return "-" if x is None else f"{x * 100:+.{digits}f}%"


def _money(x: float | None) -> str:
    return "-" if x is None else f"{x:,.0f}"


def _d(v: Any) -> str:
    return str(v)[:10]


@dataclass
class DailyReport:
    trade_date: date
    equity: float = 0.0
    cash: float = 0.0
    market_value: float = 0.0
    day_return: float | None = None
    total_return: float | None = None
    max_drawdown: float | None = None
    position_count: int = 0
    regime: str = ""
    trades: list[dict] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)
    risk_events: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    llm_cost: float = 0.0
    health: dict[str, Any] = field(default_factory=dict)
    lessons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 派生
    @property
    def turnover(self) -> float:
        return sum(float(t.get("amount") or 0) for t in self.trades)

    @property
    def fees(self) -> float:
        return sum(float(t.get("total_cost") or 0) for t in self.trades)

    @property
    def cost_drag(self) -> float:
        """费用占权益比例——这个数字长期看比单日盈亏更重要。"""
        return self.fees / self.equity if self.equity > 0 else 0.0

    @property
    def realized_pnl(self) -> float:
        return sum(float(t.get("realized_pnl") or 0) for t in self.trades)

    def to_markdown(self) -> str:
        L: list[str] = []
        L.append(f"# 交易日报 {self.trade_date}")
        L.append("")
        if self.warnings:
            L.append("## ⚠ 需要人工确认")
            L += [f"- {w}" for w in self.warnings]
            L.append("")

        L.append("## 账户")
        L.append("")
        L.append("| 指标 | 数值 |")
        L.append("|---|---|")
        L.append(f"| 总权益 | {_money(self.equity)} |")
        L.append(f"| 可用现金 | {_money(self.cash)} |")
        L.append(f"| 持仓市值 | {_money(self.market_value)}（{self.position_count} 只） |")
        L.append(f"| 当日收益 | {_pct(self.day_return)} |")
        L.append(f"| 累计收益 | {_pct(self.total_return)} |")
        L.append(f"| 最大回撤 | {_pct(self.max_drawdown)} |")
        L.append(f"| 市场状态 | {self.regime or '-'} |")
        L.append("")

        L.append("## 交易")
        L.append("")
        if not self.trades:
            L.append("_今日无成交_")
        else:
            L.append(f"成交 {len(self.trades)} 笔，成交额 {_money(self.turnover)}，"
                     f"费用 {_money(self.fees)}（占权益 {_pct(self.cost_drag, 3)}），"
                     f"已实现盈亏 {_money(self.realized_pnl)}")
            L.append("")
            L.append("| 标的 | 方向 | 价格 | 数量 | 金额 | 费用 | 实现盈亏 |")
            L.append("|---|---|---|---|---|---|---|")
            for t in self.trades:
                L.append(f"| {t.get('symbol')} | {t.get('side')} | "
                         f"{float(t.get('price') or 0):.2f} | {int(t.get('volume') or 0)} | "
                         f"{_money(t.get('amount'))} | {_money(t.get('total_cost'))} | "
                         f"{_money(t.get('realized_pnl')) if t.get('realized_pnl') is not None else '-'} |")
        L.append("")

        L.append("## 持仓")
        L.append("")
        if not self.positions:
            L.append("_空仓_")
        else:
            L.append("| 标的 | 数量 | 成本 | 现价 | 浮盈 | 止损 | 行业 |")
            L.append("|---|---|---|---|---|---|---|")
            for p in self.positions:
                cost = float(p.get("avg_cost") or 0)
                last = float(p.get("last_price") or 0)
                pnl = (last / cost - 1.0) if cost > 0 and last > 0 else None
                sl = p.get("stop_loss_price")
                sl_txt = f"{float(sl):.2f}" if sl else "-"
                L.append(f"| {p.get('symbol')} | {int(p.get('volume') or 0)} | {cost:.2f} | "
                         f"{last:.2f} | {_pct(pnl)} | {sl_txt} | {p.get('industry') or '-'} |")
        L.append("")

        rejected = [o for o in self.orders
                    if str(o.get("status")) in ("REJECTED", "GUARD_BLOCKED", "FAILED")]
        L.append("## 风控与系统")
        L.append("")
        L.append(f"- 订单 {len(self.orders)} 笔，其中被拒/拦截 {len(rejected)} 笔")
        L.append(f"- 风控事件 {len(self.risk_events)} 条")
        L.append(f"- LLM 成本 {self.llm_cost:.2f} 元")
        if self.health:
            state = "健康" if self.health.get("healthy") else "异常"
            L.append(f"- 体检: {state}"
                     + ("（已降级 REDUCE_ONLY）" if self.health.get("degraded") else ""))
        by_rule: dict[str, int] = {}
        for e in self.risk_events:
            by_rule[str(e.get("rule"))] = by_rule.get(str(e.get("rule")), 0) + 1
        if by_rule:
            L.append("")
            L.append("| 规则 | 次数 |")
            L.append("|---|---|")
            for r, c in sorted(by_rule.items(), key=lambda kv: -kv[1]):
                L.append(f"| {r} | {c} |")
        L.append("")

        if self.lessons:
            L.append("## 复盘经验")
            L.append("")
            L += [f"- {x}" for x in self.lessons]
            L.append("")

        L.append(f"_生成于 {datetime.now():%Y-%m-%d %H:%M:%S}_")
        return "\n".join(L)

    def to_text(self, *, max_rows: int = 6) -> str:
        """推消息用的精简版——企微单条消息塞不下完整 Markdown 表格。"""
        head = (f"【日报 {self.trade_date}】权益 {_money(self.equity)} "
                f"当日 {_pct(self.day_return)} 累计 {_pct(self.total_return)}")
        parts = [head,
                 f"持仓 {self.position_count} 只 | 成交 {len(self.trades)} 笔 | "
                 f"费用 {_money(self.fees)} | LLM {self.llm_cost:.2f}元"]
        if self.trades:
            rows = [f"  {t.get('side')} {t.get('symbol')} "
                    f"{int(t.get('volume') or 0)}@{float(t.get('price') or 0):.2f}"
                    for t in self.trades[:max_rows]]
            if len(self.trades) > max_rows:
                rows.append(f"  … 另有 {len(self.trades) - max_rows} 笔")
            parts.append("\n".join(rows))
        if self.warnings:
            parts.append("⚠ " + "; ".join(self.warnings))
        return "\n".join(parts)


@dataclass
class WeeklyReport:
    start: date
    end: date
    equity_start: float = 0.0
    equity_end: float = 0.0
    days: int = 0
    trade_count: int = 0
    fees: float = 0.0
    win_rate: float | None = None
    profit_factor: float | None = None
    max_drawdown: float | None = None
    best: list[tuple[str, float]] = field(default_factory=list)
    worst: list[tuple[str, float]] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    pool_weights: dict[str, float] = field(default_factory=dict)

    @property
    def ret(self) -> float:
        return (self.equity_end / self.equity_start - 1.0) if self.equity_start > 0 else 0.0

    def to_markdown(self) -> str:
        L = [f"# 周报 {self.start} ~ {self.end}", "",
             "| 指标 | 数值 |", "|---|---|",
             f"| 期初权益 | {_money(self.equity_start)} |",
             f"| 期末权益 | {_money(self.equity_end)} |",
             f"| 区间收益 | {_pct(self.ret)} |",
             f"| 最大回撤 | {_pct(self.max_drawdown)} |",
             f"| 交易笔数 | {self.trade_count} |",
             f"| 累计费用 | {_money(self.fees)} |",
             f"| 胜率 | {_pct(self.win_rate, 1) if self.win_rate is not None else '-'} |",
             f"| 盈亏比 | {self.profit_factor:.2f} |" if self.profit_factor is not None
             else "| 盈亏比 | - |",
             ""]
        if self.best or self.worst:
            L += ["## 盈亏榜", "", "| 标的 | 实现盈亏 |", "|---|---|"]
            for s, v in self.best:
                L.append(f"| {s} | {_money(v)} |")
            for s, v in self.worst:
                L.append(f"| {s} | {_money(v)} |")
            L.append("")
        if self.pool_weights:
            L += ["## 策略池权重", "", "| 策略 | 权重 |", "|---|---|"]
            for k, v in sorted(self.pool_weights.items(), key=lambda kv: -kv[1]):
                L.append(f"| {k} | {v:.1%} |")
            L.append("")
        if self.lessons:
            L += ["## 本周经验", ""] + [f"- {x}" for x in self.lessons] + [""]
        L.append(f"_生成于 {datetime.now():%Y-%m-%d %H:%M:%S}_")
        return "\n".join(L)


@dataclass
class StageReport:
    """阶段性绩效汇总。比周报看得更远：按阶段把每日收益串成曲线，
    并回答"选股到底有没有用"（命中率）与"哪些因子在失效"（IC 趋势）。"""

    start: date
    end: date
    days: int = 0
    daily_returns: list[tuple[str, float]] = field(default_factory=list)
    cum_return: float = 0.0
    ann_return: float | None = None
    sharpe: float | None = None
    max_drawdown: float = 0.0
    win_rate: float | None = None
    best_day: tuple[str, float] | None = None
    worst_day: tuple[str, float] | None = None
    fees: float = 0.0
    #: 阶段内因子 IC 均值（正=因子有效，持续为负=该降权）
    factor_ic_avg: dict[str, float] = field(default_factory=dict)
    #: 选股命中率：hit_days/eval_days，top_avg vs all_avg
    selection_hit: dict[str, float] = field(default_factory=dict)
    pool_weights: dict[str, float] = field(default_factory=dict)
    lessons: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float | None:
        n = self.selection_hit.get("eval_days") or 0
        return self.selection_hit["hit_days"] / n if n > 0 else None

    def to_markdown(self) -> str:
        L = [f"# 阶段分析报告 {self.start} ~ {self.end}", "",
             "## 绩效汇总", "", "| 指标 | 数值 |", "|---|---|",
             f"| 交易日数 | {self.days} |",
             f"| 区间收益 | {_pct(self.cum_return)} |",
             f"| 年化收益 | {_pct(self.ann_return)} |",
             f"| 夏普 | {f'{self.sharpe:.2f}' if self.sharpe is not None else '-'} |",
             f"| 最大回撤 | {_pct(-self.max_drawdown)} |",
             f"| 日胜率 | {_pct(self.win_rate, 1) if self.win_rate is not None else '-'} |",
             f"| 累计费用 | {_money(self.fees)} |"]
        if self.best_day:
            L.append(f"| 最佳交易日 | {self.best_day[0]}（{_pct(self.best_day[1])}） |")
        if self.worst_day:
            L.append(f"| 最差交易日 | {self.worst_day[0]}（{_pct(self.worst_day[1])}） |")
        L.append("")

        if self.daily_returns:
            L += ["## 每日收益", "", "| 日期 | 收益 |", "|---|---|"]
            L += [f"| {d} | {_pct(r)} |" for d, r in self.daily_returns]
            L.append("")

        hr = self.hit_rate
        if hr is not None:
            L += ["## 选股质量（Top-K 命中率）", "",
                  f"评估 {int(self.selection_hit.get('eval_days', 0))} 期，命中 "
                  f"{int(self.selection_hit.get('hit_days', 0))} 期，命中率 {hr:.0%}",
                  f"（Top-K 平均前向收益 {_pct(self.selection_hit.get('top_avg'))} "
                  f"vs 截面平均 {_pct(self.selection_hit.get('all_avg'))}）", ""]

        if self.factor_ic_avg:
            L += ["## 因子 IC 趋势（阶段均值）", "", "| 因子 | IC |", "|---|---|"]
            for k, v in sorted(self.factor_ic_avg.items(), key=lambda kv: -abs(kv[1])):
                flag = " ⚠持续为负" if v <= -0.05 else ""
                L.append(f"| {k} | {v:+.3f}{flag} |")
            L.append("")

        if self.pool_weights:
            L += ["## 策略池权重", "", "| 策略 | 权重 |", "|---|---|"]
            for k, v in sorted(self.pool_weights.items(), key=lambda kv: -kv[1]):
                L.append(f"| {k} | {v:.1%} |")
            L.append("")

        if self.lessons:
            L += ["## 阶段内经验（WARN/CRITICAL）", ""] + [f"- {x}" for x in self.lessons] + [""]
        L.append(f"_生成于 {datetime.now():%Y-%m-%d %H:%M:%S}_")
        return "\n".join(L)

    def to_text(self) -> str:
        hr = self.hit_rate
        return (f"【阶段报告 {self.start}~{self.end}】收益 {_pct(self.cum_return)} "
                f"年化 {_pct(self.ann_return)} 夏普 "
                f"{f'{self.sharpe:.2f}' if self.sharpe is not None else '-'} "
                f"回撤 {_pct(-self.max_drawdown)}"
                + (f" 选股命中率 {hr:.0%}" if hr is not None else ""))


class Reporter:
    """报告生成器。数据源全部走 repos，不依赖任何运行时内存状态。"""

    def __init__(self, settings=None, *, repos=None, notifier=None):
        cfg = (settings.section("ops").get("report", {}) if settings is not None else {})
        cfg = cfg if isinstance(cfg, dict) else {}
        self.settings = settings
        self.repos = repos
        self.notifier = notifier
        base = Path(settings.data_dir) if settings is not None else Path("data")
        self.output_dir = Path(cfg.get("output_dir") or (base / "reports"))
        self.top_n = int(cfg.get("top_n", 10))

    # ------------------------------------------------------------ 日报
    def daily(self, trade_date: date | None = None, *,
              health: dict | None = None,
              lessons: Iterable[str] = ()) -> DailyReport:
        d = trade_date or date.today()
        rep = DailyReport(trade_date=d, health=health or {},
                          lessons=list(lessons))
        if self.repos is None:
            return rep

        hist = self.repos.snapshots.history(limit=1000)
        cur = next((r for r in reversed(hist) if _d(r["trade_date"]) <= d.isoformat()), None)
        if cur:
            rep.equity = float(cur["total_asset"])
            rep.cash = float(cur["cash"])
            rep.market_value = float(cur.get("market_value") or 0)
            rep.position_count = int(cur.get("position_count") or 0)
            rep.regime = str(cur.get("regime") or "")
            idx = hist.index(cur)
            if idx > 0:
                prev = float(hist[idx - 1]["total_asset"])
                rep.day_return = (rep.equity / prev - 1.0) if prev > 0 else None
            first = float(hist[0]["total_asset"])
            rep.total_return = (rep.equity / first - 1.0) if first > 0 else None
            eq = [float(r["total_asset"]) for r in hist[: idx + 1]]
            rep.max_drawdown = -_max_dd(eq)

        rep.trades = self.repos.trades.list_by_date(d)
        rep.orders = self.repos.orders.list_by_date(d)
        rep.risk_events = self.repos.risk_events.list_by_date(d)
        rep.positions = self.repos.positions.list_all()
        try:
            rep.llm_cost = float(self.repos.llm_calls.cost_on(d))
        except Exception:
            rep.llm_cost = 0.0

        rep.warnings = self._warnings(d, rep)
        return rep

    def _warnings(self, d: date, rep: DailyReport) -> list[str]:
        out: list[str] = []
        try:
            row = self.repos.db.query_one(
                "SELECT * FROM reconcile_logs WHERE trade_date=? "
                "ORDER BY created_at DESC LIMIT 1", (d.isoformat(),))
            if row and not int(row.get("passed") or 0):
                out.append("对账未通过，次日禁止开仓直到人工确认")
        except Exception:
            pass
        try:
            ks = self.repos.system.get("killswitch")
            if ks and ks != "NORMAL":
                out.append(f"KillSwitch 处于 {ks}")
        except Exception:
            pass
        sev = [e for e in rep.risk_events
               if str(e.get("severity")).upper() in ("ERROR", "CRITICAL")]
        if sev:
            out.append(f"{len(sev)} 条严重风控事件")
        if rep.equity > 0 and rep.cost_drag > 0.006:
            out.append(f"当日费用占权益 {rep.cost_drag:.2%}，交易过于频繁")
        return out

    # ------------------------------------------------------------ 周报
    def weekly(self, end: date | None = None, *, days: int = 7,
               lessons: Iterable[str] = (),
               pool_weights: dict[str, float] | None = None) -> WeeklyReport:
        e = end or date.today()
        s = e - timedelta(days=days - 1)
        rep = WeeklyReport(start=s, end=e, lessons=list(lessons),
                           pool_weights=dict(pool_weights or {}))
        if self.repos is None:
            return rep

        hist = [r for r in self.repos.snapshots.history(limit=2000)
                if s.isoformat() <= _d(r["trade_date"]) <= e.isoformat()]
        if hist:
            rep.equity_start = float(hist[0]["total_asset"])
            rep.equity_end = float(hist[-1]["total_asset"])
            rep.days = len(hist)
            rep.max_drawdown = -_max_dd([float(r["total_asset"]) for r in hist])

        trades = [t for t in self.repos.trades.list_all()
                  if s.isoformat() <= _d(t["trade_date"]) <= e.isoformat()]
        rep.trade_count = len(trades)
        rep.fees = sum(float(t.get("total_cost") or 0) for t in trades)

        closed = [t for t in trades if t.get("realized_pnl") is not None]
        if closed:
            pnls = [float(t["realized_pnl"]) for t in closed]
            wins = [p for p in pnls if p > 0]
            losses = [-p for p in pnls if p < 0]
            rep.win_rate = len(wins) / len(pnls)
            rep.profit_factor = (sum(wins) / sum(losses)) if losses else float("inf")
            by_sym: dict[str, float] = {}
            for t in closed:
                by_sym[t["symbol"]] = by_sym.get(t["symbol"], 0.0) + float(t["realized_pnl"])
            ranked = sorted(by_sym.items(), key=lambda kv: -kv[1])
            k = min(3, len(ranked))
            rep.best = ranked[:k]
            rep.worst = [x for x in ranked[-k:] if x not in ranked[:k]][::-1]
        return rep

    # ------------------------------------------------------------ 阶段报告
    def stage(self, end: date | None = None, *, days: int = 30) -> StageReport:
        """阶段性汇总：把窗口内的每日收益串起来，并聚合选股命中率、
        因子 IC 趋势、策略池状态与阶段内经验。数据全部来自库（P6）。"""
        e = end or date.today()
        s = e - timedelta(days=days - 1)
        rep = StageReport(start=s, end=e)
        if self.repos is None:
            return rep

        hist = [r for r in self.repos.snapshots.history(limit=2000)
                if s.isoformat() <= _d(r["trade_date"]) <= e.isoformat()]
        # 日收益需要窗口前一天的权益作基准，所以多取一条
        full = self.repos.snapshots.history(limit=2000)
        prior = next((r for r in reversed(full)
                      if _d(r["trade_date"]) < s.isoformat()), None)
        chain = ([prior] if prior else []) + hist
        rets: list[tuple[str, float]] = []
        for i in range(1, len(chain)):
            prev = float(chain[i - 1]["total_asset"])
            if prev > 0:
                rets.append((_d(chain[i]["trade_date"]),
                             float(chain[i]["total_asset"]) / prev - 1.0))
        rep.daily_returns = rets
        rep.days = len(hist)
        if rets:
            cum = 1.0
            for _, r in rets:
                cum *= 1.0 + r
            rep.cum_return = cum - 1.0
            n = len(rets)
            rep.ann_return = cum ** (244 / n) - 1.0 if cum > 0 else None
            arr = [r for _, r in rets]
            mean = sum(arr) / n
            var = sum((r - mean) ** 2 for r in arr) / n
            sd = math.sqrt(var)
            rep.sharpe = (mean / sd * math.sqrt(244)) if sd > 1e-12 else None
            rep.win_rate = sum(1 for r in arr if r > 0) / n
            rep.best_day = max(rets, key=lambda t: t[1])
            rep.worst_day = min(rets, key=lambda t: t[1])
        if hist:
            rep.max_drawdown = _max_dd([float(r["total_asset"]) for r in chain[1:]])

        try:
            trades = [t for t in self.repos.trades.list_all()
                      if s.isoformat() <= _d(t["trade_date"]) <= e.isoformat()]
            rep.fees = sum(float(t.get("total_cost") or 0) for t in trades)
        except Exception:                            # noqa: BLE001
            pass

        rep.factor_ic_avg = self._factor_ic_avg(s, e)
        rep.selection_hit = self._selection_hit(s, e)
        rep.pool_weights = self._pool_weights()
        try:
            rows = self.repos.experiences.recent(s, limit=100, tags_like="WARN")
            seen: set[str] = set()
            for r in rows:
                txt = r.get("lesson") or r.get("situation") or ""
                if txt and txt not in seen:
                    seen.add(txt)
                    rep.lessons.append(f"[{_d(r['trade_date'])}] {txt}")
            rep.lessons = rep.lessons[:12]
        except Exception:                            # noqa: BLE001
            pass
        return rep

    def _factor_ic_avg(self, s: date, e: date) -> dict[str, float]:
        """阶段内因子 IC 均值。连续为负的因子会在阶段报告里被点名。"""
        try:
            keys = [k for k in self.repos.system.list_keys("evolution:factor_ic:")
                    if s.isoformat() <= k.rsplit(":", 1)[-1] <= e.isoformat()]
            acc: dict[str, list[float]] = {}
            for k in keys:
                try:
                    ic = json.loads(self.repos.system.get(k) or "{}")
                except Exception:                    # noqa: BLE001
                    continue
                for f, v in ic.items():
                    try:
                        acc.setdefault(f, []).append(float(v))
                    except (TypeError, ValueError):
                        continue
            return {f: round(sum(vs) / len(vs), 4) for f, vs in acc.items()}
        except Exception:                            # noqa: BLE001
            return {}

    def _selection_hit(self, s: date, e: date) -> dict[str, float]:
        """聚合阶段内每期 Top-K 命中率（review job 按日落库 selection:hit:*）。"""
        try:
            keys = [k for k in self.repos.system.list_keys("selection:hit:")
                    if s.isoformat() <= k.rsplit(":", 1)[-1] <= e.isoformat()]
            n = hits = 0
            tops: list[float] = []
            alls: list[float] = []
            for k in keys:
                try:
                    h = json.loads(self.repos.system.get(k) or "{}")
                except Exception:                    # noqa: BLE001
                    continue
                if "top_avg" not in h or "all_avg" not in h:
                    continue
                n += 1
                hits += 1 if h.get("hit") else 0
                tops.append(float(h["top_avg"]))
                alls.append(float(h["all_avg"]))
            if n == 0:
                return {}
            return {"eval_days": float(n), "hit_days": float(hits),
                    "top_avg": sum(tops) / n, "all_avg": sum(alls) / n}
        except Exception:                            # noqa: BLE001
            return {}

    def _pool_weights(self) -> dict[str, float]:
        try:
            raw = self.repos.system.get("strategy_pool")
            snap = json.loads(raw) if raw else {}
            return {n: float(d.get("weight", 0))
                    for n, d in (snap.get("strategies") or {}).items()
                    if float(d.get("weight", 0)) > 0}
        except Exception:                            # noqa: BLE001
            return {}

    # ------------------------------------------------------------ 输出
    def save(self, rep: DailyReport | WeeklyReport | StageReport, *, fmt: str = "md") -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(rep, DailyReport):
            name = f"daily_{rep.trade_date:%Y%m%d}.{fmt}"
        elif isinstance(rep, WeeklyReport):
            name = f"weekly_{rep.start:%Y%m%d}_{rep.end:%Y%m%d}.{fmt}"
        else:
            name = f"stage_{rep.start:%Y%m%d}_{rep.end:%Y%m%d}.{fmt}"
        path = self.output_dir / name
        path.write_text(rep.to_markdown(), encoding="utf-8")
        logger.info("报告已写入 %s", path)
        return path

    def push(self, rep: DailyReport | WeeklyReport | StageReport) -> bool:
        if self.notifier is None:
            return False
        if isinstance(rep, DailyReport):
            # 有需人工确认的事项时抬高级别，否则日报只是 INFO
            level = "WARN" if rep.warnings else "INFO"
            return self.notifier.notify(f"交易日报 {rep.trade_date}",
                                        rep.to_text(), level=level,
                                        key=f"daily:{rep.trade_date}")
        if isinstance(rep, WeeklyReport):
            return self.notifier.notify(
                f"周报 {rep.start}~{rep.end}",
                f"区间收益 {_pct(rep.ret)}，交易 {rep.trade_count} 笔，费用 {_money(rep.fees)}",
                level="INFO", key=f"weekly:{rep.end}")
        return self.notifier.notify(f"阶段报告 {rep.start}~{rep.end}",
                                    rep.to_text(), level="INFO",
                                    key=f"stage:{rep.end}")


def _max_dd(equity: list[float]) -> float:
    """返回最大回撤幅度（正数）。"""
    if not equity:
        return 0.0
    peak, dd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd
