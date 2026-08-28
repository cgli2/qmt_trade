"""回测引擎（设计 6.9 / P7）。

把 SelectionPipeline → RiskEngine → PositionSizer → ExecutionService →
SimGateway 串成一条**与实盘同代码路径**的回测闭环。这是 P7 的核心交付：
回测跑出来的结果能直接信，因为执行/风控/仓位逻辑和实盘是同一份。

防未来函数纪律（PIT）：
- 选股决策在 T 日收盘后做，因子只用到 T-1（SelectionPipeline 内部 as_of_pre_open 已保证）。
- 所有订单（新开仓 + 止损）统一在 **T+1 日开盘**撮合。
- Gate-2 持仓守护在 T 日用 T 日 bar 判定，触发后同样 T+1 开盘卖出。
- day/five-day 亏损熔断由权益曲线派生（修正 1 日执行滞后的计数偏差）。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from ..brain.schemas import TradeIntent, intent_from_rank
from ..brain.state import PortfolioSnapshot
from ..core.errors import DataUnavailableError
from ..core.trading import Side
from ..datahub.manager import DataHub
from ..datahub.types import Adjust, Bar, Freq
from ..execution.costs import CostModel
from ..execution.gateway.simulator import SimGateway
from ..execution.order_guard import OrderGuard
from ..execution.service import ExecutionService
from ..features.regime import Regime, RegimeSnapshot
from ..portfolio.sizer import PositionSizer
from ..portfolio.state import PortfolioState
from ..risk.engine import RiskEngine
from ..risk.killswitch import KillSwitch
from ..selection import SelectionPipeline
from .metrics import performance

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    equity_curve: list[float] = field(default_factory=list)
    trades: list = field(default_factory=list)          # 逐笔成交（Fill）
    closed_trades: list = field(default_factory=list)   # 平仓明细（round-trip），喂 L5 复盘
    open_positions: list = field(default_factory=list)  # 期末未平仓持仓快照（含浮盈），供"含浮盈组合胜率"
    metrics: dict = field(default_factory=dict)
    details: list = field(default_factory=list)


class BacktestEngine:
    def __init__(self, settings, hub: DataHub, *, initial_cash: float = 1_000_000.0,
                 top_n: int = 10, max_holding_days: int = 20, fixed_start: date | None = None,
                 brain=None, strategy: str | None = None):
        """
        Parameters
        ----------
        brain : BrainGraph | None
            为 None 时走 ``intent_from_rank`` 的纯因子路径（P5 默认）。
            传入 BrainGraph 则由多智能体产出 Intent —— **下游完全一样**，
            这正是 P7 的验证点：换脑不换执行链路。
        strategy : str | None
            策略预设 id（core/strategies.py）。None = 原始默认行为，保证
            历史回测结果可复现；传入后选股权重/入选门槛按预设覆盖。
        """
        self.settings = settings
        self.hub = hub
        self.initial_cash = initial_cash
        self.top_n = top_n
        self.max_holding_days = max_holding_days
        self.fixed_start = fixed_start
        self.brain = brain
        self.strategy = strategy
        # ---- 入场/出场改良开关（默认全部关闭 = 与旧行为完全一致）----
        bcfg = settings.section("backtest") or {}
        # 调仓频率：daily 每日选股（旧行为）/ weekly 每周首个交易日选股换仓，
        # 其余交易日只做持仓守护 —— 把换手成本侵蚀压到 0.5pp 以内
        self.rebalance = str(bcfg.get("rebalance", "daily")).lower()
        self._last_rebal_week: tuple[int, int] | None = None
        # T+1 开盘价较 T 日收盘高开超此比例则放弃买入（防追高）；0/None=不过滤
        self.max_entry_gap = float(bcfg.get("max_entry_gap", 0) or 0)
        # 已持仓标的再次入选时不重复加仓（降换手）
        self.skip_held_rebuy = bool(bcfg.get("skip_held_rebuy", False))
        # 整仓止盈（V6）：>0 时给买入意图挂一个到价兑现档，修复"盈利永不兑现、
        # 平仓胜率结构性≈0"的缺陷。0/None = 维持原行为（仅靠止损/时间止损平仓）。
        self.take_profit_pct = float(bcfg.get("take_profit_pct", 0) or 0)
        # 止损：FIXED_PCT 固定百分比 / ATR 按 1.5×ATR(20) 自适应折算
        self.stop_mode = str(bcfg.get("stop_mode", "FIXED_PCT")).upper()
        self.stop_pct = float(bcfg.get("stop_pct", 0.07))
        self.stop_atr_mult = float(bcfg.get("stop_atr_mult", 1.5))
        # 当日止损：盘中 low 触及止损价即按止损价当日成交（低开穿价按开盘价），
        # 替代「收盘确认 + 次日开盘补刀」—— 次日跳空补刀会把 7% 止损放大成 -9%+
        self.stop_same_day = bool(bcfg.get("stop_same_day", False))
        # 入场趋势确认：off 关闭 / ma20 仅要求收盘站上 MA20 /
        # strict 另要求 MA20 近 5 日向上（拦掉下跌趋势/破位入场，止损单主源）
        self.entry_trend_filter = str(bcfg.get("entry_trend_filter", "off")).lower()
        # 入场缺口/波动风险过滤（off/on）：拦截"爱跳空、高波动、近期有深跌"
        # 的标的 —— 长区间归因显示 14 笔止损中 6 笔被开盘跳穿到 -11%~-16%，
        # 当日止损只能接盘中触价、接不住跳空，只能从入场端规避这类标的
        self.entry_risk_filter = str(bcfg.get("entry_risk_filter", "off")).lower()
        # 跳空日判定阈值：|open/前收 - 1| 超此幅度记一次跳空
        self.risk_gap_pct = float(bcfg.get("entry_risk_gap_pct", 0.03))
        # 近 20 日跳空日次数上限（含），超过则放弃入场
        self.risk_max_gap_days = int(bcfg.get("entry_risk_max_gap_days", 3))
        # ATR(20)/收盘 上限，高波动标的止损必然被反复扫
        self.risk_max_atr = float(bcfg.get("entry_risk_max_atr", 0.045))
        # 近 20 日允许的单日最大跌幅（绝对值），出现更深跌幅说明有崩跌史
        self.risk_max_drop = float(bcfg.get("entry_risk_max_drop", 0.08))
        # 持仓期缺口守护（off/on）：持仓标的开盘较前收跳空低开超阈值时，
        # 当日开盘即清仓 —— 当日止损只能接住盘中触价，接不住开盘直接
        # 跳穿止损（V9 残留 688378.SH -17.37% 漏损即此类）
        self.gap_guard = str(bcfg.get("gap_guard", "off")).lower()
        # 缺口守护触发阈值：开盘跳空低开幅度超此值（正数）即当日开盘离场
        self.gap_guard_pct = float(bcfg.get("gap_guard_pct", 0.05))
        # 最近一次选股判定的 Regime（Gate-2 用它做真实的降仓守护）
        self._cur_regime: RegimeSnapshot | None = None
        self.pipeline = SelectionPipeline(settings, hub)
        self.cost = CostModel.from_settings(settings)
        self.killswitch = KillSwitch()
        self.risk = RiskEngine(settings)
        self.sizer = PositionSizer(settings)
        self.guard = OrderGuard(settings)
        self.portfolio = PortfolioState(cash=initial_cash)
        self.exec_svc = ExecutionService(
            settings, SimGateway(), self.cost, self.guard, self.risk,
            self.killswitch, self.portfolio, sizer=self.sizer)
        self.universe: list[str] = []

    # ------------------------------------------------------------ 主循环
    def run(self, start: date, end: date) -> BacktestResult:
        days = self._trading_days(start, end)
        if len(days) < 3:
            return BacktestResult(details=["交易日不足"])
        self.universe = self._universe(days[0])
        # 固定历史起点：所有逐票取数（趋势确认/缺口体检/ATR止损/撮合/市值）都用这个
        # 稳定起点，让 DataHub 范围缓存按 (标的, 起点, 复权) 命中。否则滑动起点
        # （每日 d-60 / day）导致缓存键每天变化，每只候选票每天都全量重拉行情
        # —— 这是回测下载慢的主因。fixed_start 由 CLI 的 start-warmup 保证传入，
        # 这里仅做兜底（极端情况下退化为全段前 1 年，仍比每日滑动好）。
        if self.fixed_start is None:
            self.fixed_start = days[0] - timedelta(days=365)
        # 性能优化（2026-08-12）：预热全量行情到 DataHub 范围缓存。
        # 一次拉 [fixed_start, end] 全量（~20-30s），后续每天 build_panel 命中
        # 缓存按 asof 切片，避免 49 天 × 每天全量重拉（~16 分钟 → ~1 分钟）。
        if self.fixed_start:
            try:
                self.hub.get_bars(self.universe, Freq.D1, self.fixed_start, end,
                                  Adjust.HFQ, validate=True)
            except Exception as exc:  # noqa: BLE001 - 预热失败不阻塞回测，逐日取数兜底
                logger.warning("行情预热失败（回测继续，逐日取数）: %s", exc)
        # 行情内存索引（提速 2026-08-13）：把预热拿到的全量日线按 (标的, 复权) 建内存索引，
        # 回测循环里的 _bar / _last_prices / 趋势&风险体检 直接切片索引，
        # 不再逐标的回源 get_bars（避免数千次单标的取数 / 重新落盘读盘）。
        self._bt_end = end
        self._bars_index: dict = {}
        for _adj in (Adjust.NONE, Adjust.HFQ):
            try:
                _df = self.hub.get_bars(self.universe, Freq.D1, self.fixed_start, end,
                                        _adj, validate=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("行情预热(%s)失败，回退逐票取数: %s", _adj.value, exc)
                _df = None
            if _df is not None and not _df.empty:
                _df = _df.copy()
                _df["date"] = pd.to_datetime(_df["date"])
                self._bars_index[_adj] = {
                    s: sub.sort_values("date").reset_index(drop=True)
                    for s, sub in _df.groupby("symbol")
                }

        # 财务预热：一次性把全 universe 财务下载到 QMT 本地磁盘（首次大 universe
        # 下载较慢，集中在这里、给足超时；之后每个调仓日走本地缓存 + 进程内缓存，秒回）。
        qmt = getattr(self.hub, "providers", {}).get("qmt")
        if qmt is not None and hasattr(qmt, "prewarm_fundamentals"):
            try:
                n = qmt.prewarm_fundamentals(self.universe)
                logger.info("财务预热：QMT 提供 %d 只标的财务", n)
            except Exception as exc:  # noqa: BLE001 - 预热失败不阻塞，逐调仓日取数兜底
                logger.warning("财务预热失败（回测继续，逐调仓日取数）: %s", exc)
        self.guard.on_new_day()
        instrument_map = self._instrument_map(self.universe)
        sym_industry = {s: getattr(i, "industry", "") for s, i in instrument_map.items()}

        result = BacktestResult()
        logger.info("回测启动 strategy=%s rebalance=%s stop=%s(%s) entry_gap=%.2f%% rebuy_skip=%s",
                    self.strategy or "(default)", self.rebalance, self.stop_mode,
                    self.stop_pct if self.stop_mode == "FIXED_PCT" else f"{self.stop_atr_mult}×ATR",
                    self.max_entry_gap * 100, self.skip_held_rebuy)
        for i, d in enumerate(days[:-1]):
            next_day = days[i + 1]
            # 由权益曲线派生当日/近5日亏损（修正执行滞后），喂给风控熔断
            self._derive_loss_limits()
            # 解锁隔夜仓（T+1），使 Gate-1 的「可卖数量校验」能放行
            self.portfolio.mark_t1(next_day)

            # --- Gate-2：T 日持仓守护（用 T 日 bar 判定）---
            # Regime 用上一决策日选股的判定结果（≤ d-1 信息，无穿越）；
            # 首日尚未选股时降级为 RANGE 快照
            guard_regime = self._cur_regime or self._range_snap(d)
            last_t = self._last_prices(d)
            self.portfolio.refresh(last_t)
            # --- 持仓期缺口守护：开盘跳空低开超阈值，当日开盘即离场（早于当日止损/Gate-2）---
            if self.gap_guard != "off":
                self._gap_guard(d, days[i - 1] if i > 0 else None, guard_regime,
                                instrument_map, sym_industry, result)
            # --- 当日止损：盘中触价立即成交（在 Gate-2 之前，已卖标的守护自动跳过）---
            if self.stop_same_day:
                self._intraday_stops(d, guard_regime, instrument_map, sym_industry,
                                     result)
            actions = self.risk.guard_positions(
                self.portfolio, last_prices=last_t, asof=d,
                killswitch=self.killswitch, regime=guard_regime)
            for ga in actions:
                sym = ga.symbol
                pos = self.portfolio.positions.get(sym)
                if not pos:
                    continue
                sell = TradeIntent(
                    symbol=sym, action=ga.action if ga.action in ("SELL", "REDUCE") else "SELL",
                    confidence=1.0, conviction="HIGH",
                    stop_loss_type="FIXED_PCT", stop_loss_value=0.0,
                    max_weight_hint=0.3, max_holding_days=self.max_holding_days,
                    valid_until=next_day, reasoning=ga.reason)
                bar = self._bar(sym, next_day)
                if bar is None:
                    continue
                res = self.exec_svc.submit_intent(
                    sell, bar=bar, market_day=next_day, asof=d,
                    regime=guard_regime, instrument=instrument_map.get(sym),
                    sym_industry=sym_industry, plan_id=f"stop_{d.isoformat()}", seq=0,
                    signal=ga.tag, reduce_shares=ga.shares)
                if res.ok:
                    result.trades.append(res.fill)

            # --- 选股（T 日决策，因子用 ≤ T-1）---
            # weekly：仅每周首个交易日选股换仓，其余交易日跳过（持仓守护已在上方完成）；
            # Gate-2 沿用上一调仓日的 Regime 判定（滞后 ≤ 1 周，无穿越）
            iso = d.isocalendar()
            is_rebal_day = (self.rebalance != "weekly"
                            or (iso[0], iso[1]) != self._last_rebal_week)
            daily_open = 0
            cs = None
            if is_rebal_day:
                if self.rebalance == "weekly":
                    self._last_rebal_week = (iso[0], iso[1])
                    logger.info("周度调仓日 %s", d.isoformat())
                try:
                    cs = self.pipeline.run(d, universe=self.universe, history_start=self.fixed_start,
                                           strategy=self.strategy)
                except DataUnavailableError as exc:
                    # 全部行情源不可用：当日停止开仓（P4），持仓守护/权益记录照常
                    logger.warning("决策日 %s 全部数据源不可用，当日停止开仓: %s", d, exc)
                    cs = None
            if cs is not None:
                self._cur_regime = cs.regime
            if cs is not None and not cs.is_empty:
                for rank, (sym, intent) in enumerate(
                        self._make_intents(cs, d, next_day, sym_industry), start=1):
                    if daily_open >= self.risk.max_daily_open:
                        break
                    if self.skip_held_rebuy and sym in self.portfolio.positions:
                        continue  # 已持仓不重复加仓，降低换手
                    if self.entry_trend_filter != "off" and not self._trend_ok(sym, d):
                        logger.info("趋势确认未通过，放弃 %s（%s）", sym, d.isoformat())
                        continue
                    if self.entry_risk_filter != "off" and not self._entry_risk_ok(sym, d):
                        logger.info("缺口/波动风险过滤，放弃 %s（%s）", sym, d.isoformat())
                        continue
                    bar = self._bar(sym, next_day)
                    if bar is None:
                        continue
                    if self.max_entry_gap > 0 and bar.open > 0:
                        # 防追高：T+1 开盘较 T 日收盘高开超限则放弃当日买入
                        prev_bar = self._bar(sym, d)
                        if (prev_bar is not None and prev_bar.close > 0
                                and bar.open > prev_bar.close * (1 + self.max_entry_gap)):
                            logger.info("追高过滤 %s：开盘 %.2f 高于前收 %.2f 超 %.0f%%",
                                        sym, bar.open, prev_bar.close, self.max_entry_gap * 100)
                            continue
                    if intent.entry_ref_price is None:
                        intent = intent.model_copy(
                            update={"entry_ref_price": bar.open or bar.close})
                    intent = self._apply_stop(intent, sym, d, bar)
                    res = self.exec_svc.submit_intent(
                        intent, bar=bar, market_day=next_day, asof=d,
                        regime=cs.regime, instrument=instrument_map.get(sym),
                        sym_industry=sym_industry, daily_open_count=daily_open,
                        plan_id=f"sel_{d.isoformat()}", seq=rank)
                    if res.ok:
                        daily_open += 1
                        result.trades.append(res.fill)

            # --- 记录权益（T+1 收盘市值）---
            last_next = self._last_prices(next_day)
            self.portfolio.refresh(last_next)
            self.portfolio.record_equity(day_end=True)
            result.equity_curve.append(round(self.portfolio.total_asset, 2))
            result.details.append({
                "date": d.isoformat(), "asset": round(self.portfolio.total_asset, 2),
                "positions": len(self.portfolio.positions),
                "killswitch": self.killswitch.mode.value,
            })

        result.closed_trades = list(self.portfolio.closed_trades)
        # 期末未平仓持仓快照：win_rate 只统计已平仓（realized），会系统性低估
        # "持有至窗口末的盈利仓"。这里把未平仓仓的浮盈一并暴露，供 cli 计算
        # "含浮盈组合胜率"，解开"组合盈利但 win_rate=0"的假象。
        result.open_positions = [
            {
                "symbol": s,
                "avg_cost": round(p.avg_cost, 4),
                "last_price": round(p.last_price or p.avg_cost, 4),
                "shares": p.shares,
                "highest_since_open": round(p.highest_since_open or p.avg_cost, 4),
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
            }
            for s, p in self.portfolio.positions.items()
        ]
        result.metrics = performance(result.equity_curve, trades=result.trades,
                                      realized_log=self.portfolio.realized_log)
        return result

    # ------------------------------------------------------------ 出场改良
    def _intraday_stops(self, d: date, regime, instrument_map: dict,
                        sym_industry: dict[str, str], result: BacktestResult) -> None:
        """盘中止损：当日 low ≤ 止损价即成交。成交价 = min(开盘价, 止损价)
        （跳空低开按开盘价，否则假定盘中能在止损价卖出）。只用 ≤ d 日数据，
        无未来函数；T+1 当日买入的仓位（opened_at == d）不可卖，跳过。"""
        for sym, pos in list(self.portfolio.positions.items()):
            if not pos.stop_loss_price or pos.shares <= 0:
                continue
            if not pos.opened_at or (d - pos.opened_at).days < 1:
                continue  # T+1：当日建仓不可卖
            bar = self._bar(sym, d)
            if bar is None:
                continue
            # 脏数据兜底：low 非正（源数据质量问题）时用 open/close 较低者近似，
            # 否则止损整天被跳过，最终在深跌日才触发（V8 首跑出现 -16.8% 漏损）
            low = bar.low if bar.low > 0 else min(
                x for x in (bar.open, bar.close) if x > 0) if max(bar.open, bar.close) > 0 else 0.0
            if low <= 0 or low > pos.stop_loss_price:
                continue
            fill_open = (min(bar.open, pos.stop_loss_price)
                         if bar.open > 0 else pos.stop_loss_price)
            sell = TradeIntent(
                symbol=sym, action="SELL", confidence=1.0, conviction="HIGH",
                stop_loss_type="FIXED_PCT", stop_loss_value=0.0,
                max_weight_hint=0.3, max_holding_days=self.max_holding_days,
                valid_until=d,
                reasoning=f"盘中触及止损位 {pos.stop_loss_price:.2f}，当日离场")
            res = self.exec_svc.submit_intent(
                sell, bar=replace(bar, open=fill_open), market_day=d, asof=d,
                regime=regime, instrument=instrument_map.get(sym),
                sym_industry=sym_industry, plan_id=f"stopday_{d.isoformat()}",
                seq=0, signal="STOP_LOSS")
            if res.ok:
                result.trades.append(res.fill)

    def _gap_guard(self, d: date, prev_day: date | None, regime, instrument_map: dict,
                   sym_industry: dict[str, str], result: BacktestResult) -> None:
        """持仓期缺口守护：持仓标的当日开盘较前收跳空低开超阈值，当日开盘
        即清仓。当日止损只能接住盘中触价，接不住开盘直接跳穿止损位
        （V9 残留 688378.SH -17.37% 漏损即此类）。判定只用当日开盘与前收，
        成交价即当日开盘 —— 等价预先挂好的条件单，无未来函数；T+1 当日
        建仓的仓位不可卖，跳过；取数失败/脏数据日跳过（不裸奔也不错杀）。"""
        if prev_day is None:
            return
        for sym, pos in list(self.portfolio.positions.items()):
            if pos.shares <= 0:
                continue
            if not pos.opened_at or (d - pos.opened_at).days < 1:
                continue  # T+1：当日建仓不可卖
            # 跳空判断用 HFQ 复权价：抹平除权日"假跳空"（分红除息导致的开盘跳低），
            # 只对真实的隔夜崩跌触发；成交价仍用 NONE 真实开盘价（与实盘条件单一致）。
            bar = self._bar(sym, d, adjust=Adjust.HFQ)
            prev = self._bar(sym, prev_day, adjust=Adjust.HFQ)
            if bar is None or prev is None or bar.open <= 0 or prev.close <= 0:
                continue
            gap = bar.open / prev.close - 1
            if gap > -self.gap_guard_pct:
                continue
            sell = TradeIntent(
                symbol=sym, action="SELL", confidence=1.0, conviction="HIGH",
                stop_loss_type="FIXED_PCT", stop_loss_value=0.0,
                max_weight_hint=0.3, max_holding_days=self.max_holding_days,
                valid_until=d,
                reasoning=f"开盘跳空 {gap:+.1%} 触发缺口守护，当日开盘离场")
            res = self.exec_svc.submit_intent(
                sell, bar=self._bar(sym, d), market_day=d, asof=d,
                regime=regime, instrument=instrument_map.get(sym),
                sym_industry=sym_industry, plan_id=f"gapguard_{d.isoformat()}",
                seq=0, signal="GAP_GUARD")
            if res.ok:
                result.trades.append(res.fill)

    def _trend_ok(self, sym: str, d: date) -> bool:
        """入场趋势确认（仅用 ≤ T 信息）：ma20 = close_T > MA20；
        strict 另要求 MA20 较 5 日前抬升。"""
        df = self._hist_bars(sym, Adjust.HFQ)
        if df is None or df.empty:
            return False
        df = df[df["date"] <= pd.Timestamp(d)]
        if "symbol" in df.columns:
            df = df[df["symbol"] == sym]
        df = df.sort_values("date")
        c = df["close"].astype(float).to_numpy()
        if len(c) < 25:
            return False
        ma20 = float(c[-20:].mean())
        if float(c[-1]) <= ma20:
            return False
        if self.entry_trend_filter != "strict":
            return True
        return ma20 > float(c[-25:-5].mean())

    def _entry_risk_ok(self, sym: str, d: date) -> bool:
        """入场缺口/波动风险体检（仅用 ≤ T 信息）。任一不过即放弃：
        ① 近 20 日跳空日（|open/前收-1| > gap_pct）次数超上限；
        ② ATR(20)/收盘 > 上限（高波动，7% 止损必然被反复扫穿）；
        ③ 近 20 日出现过单日跌幅超上限（崩跌史，跳空深亏的高危特征）。
        取数失败按通过处理（避免把数据故障当成过滤，趋势过滤已兼顾）。"""
        df = self._hist_bars(sym, Adjust.HFQ)
        if df is None or df.empty:
            return True
        df = df[df["date"] <= pd.Timestamp(d)]
        if "symbol" in df.columns:
            df = df[df["symbol"] == sym]
        df = df.sort_values("date")
        if len(df) < 21:
            return True
        o = df["open"].astype(float).to_numpy()
        h = df["high"].astype(float).to_numpy()
        l = df["low"].astype(float).to_numpy()
        c = df["close"].astype(float).to_numpy()
        # ① 近 20 日跳空日计数（前收≤0 的脏数据日跳过）
        gaps = sum(abs(o[i] / c[i - 1] - 1) > self.risk_gap_pct
                   for i in range(len(c) - 20, len(c)) if c[i - 1] > 0)
        if gaps > self.risk_max_gap_days:
            return False
        # ② ATR(20)/收盘
        if c[-1] > 0:
            tr = np.maximum(h[-20:] - l[-20:],
                            np.maximum(np.abs(h[-20:] - c[-21:-1]),
                                       np.abs(l[-20:] - c[-21:-1])))
            if float(np.mean(tr)) / c[-1] > self.risk_max_atr:
                return False
        # ③ 近 20 日单日最大跌幅（前收≤0 的脏数据日跳过）
        drops = [c[i] / c[i - 1] - 1 for i in range(len(c) - 20, len(c))
                 if c[i - 1] > 0 and c[i] > 0]
        if drops and min(drops) < -self.risk_max_drop:
            return False
        return True

    # ------------------------------------------------------------ Intent 来源
    def _make_intents(self, cs, d: date, next_day: date,
                      sym_industry: dict[str, str]) -> list[tuple[str, TradeIntent]]:
        """产出当日买入 Intent。两条路径产物同构，下游执行链路完全一致（P7）。"""
        if self.brain is not None:
            snap = PortfolioSnapshot.from_portfolio(
                self.portfolio, sym_industry,
                max_positions=self.risk.max_positions,
                max_position_pct=getattr(cs.regime, "max_position", 0.8))
            br = self.brain.run(cs, snap, symbols=cs.shortlist(self.top_n * 2))
            return [(it.symbol, it) for it in br.intents[:self.top_n]]

        score_by_sym = cs.frame.set_index("symbol")["score"].to_dict()
        out: list[tuple[str, TradeIntent]] = []
        for rank, sym in enumerate(cs.symbols[:self.top_n], start=1):
            out.append((sym, intent_from_rank(
                sym, rank=rank, score=float(score_by_sym.get(sym, 0.5)),
                ref_price=0.0, stop_pct=self.stop_pct, valid_until=next_day,
                industry=sym_industry.get(sym, ""),
                max_holding_days=self.max_holding_days,
                take_profit_pct=self.take_profit_pct)))
        return out

    def _apply_stop(self, intent: TradeIntent, sym: str, d: date, bar: Bar) -> TradeIntent:
        """ATR 止损模式：用真实 ATR(20) 折算成 FIXED_PCT（执行层的 ATR 分支
        只是占位实现，不能依赖）。取数不足则回退固定百分比，保守不裸奔。"""
        if self.stop_mode != "ATR":
            return intent
        entry = bar.open or bar.close
        if entry <= 0:
            return intent
        df = self._hist_bars(sym, Adjust.HFQ)
        if df is None or df.empty:
            return intent
        df = df[df["date"] <= pd.Timestamp(d)]
        df = df[df["symbol"] == sym].sort_values("date") if "symbol" in df.columns else df.sort_values("date")
        if len(df) < 21:
            return intent
        h = df["high"].astype(float).to_numpy()
        l = df["low"].astype(float).to_numpy()
        c = df["close"].astype(float).to_numpy()
        tr = np.maximum(h[-20:] - l[-20:],
                        np.maximum(np.abs(h[-20:] - c[-21:-1]), np.abs(l[-20:] - c[-21:-1])))
        gap = float(np.mean(tr) * self.stop_atr_mult) / entry
        gap = min(max(gap, 0.02), 0.20)  # 夹在 [2%, 20%]，极端行情下不失控
        return intent.model_copy(
            update={"stop_loss_type": "FIXED_PCT", "stop_loss_value": round(gap, 4)})

    # ------------------------------------------------------------ 辅助
    # 复权口径纪律（F2 修复，2026-08-12）：
    #   * 撮合（_bar）与市值标记（_last_prices）必须用 **不复权 Adjust.NONE** 真实价，
    #     与实盘执行/选股参考（selection/pipeline.py 同样 NONE）同一套语义 —— 此前默认
    #     HFQ 后复权价（datahub/manager.py get_bars 默认值），撮合与实盘是两套价，
    #     区间内分红的票回测收益会被系统性高估。
    #   * 判断类取数（趋势确认 _trend_ok / 缺口波动体检 _entry_risk_ok / ATR 止损
    #     _apply_stop / 缺口守护判断）**有意保持 HFQ**：复权价能抹平除权日的假跳空，
    #     判断更稳；但缺口守护的成交价仍用 NONE 真实开盘价（见 _gap_guard）。
    def _derive_loss_limits(self) -> None:
        eq = self.portfolio.equity_curve
        if len(eq) >= 2:
            self.portfolio.day_loss = max(0.0, eq[-2] - eq[-1])
            recent = [max(0.0, eq[-k - 1] - eq[-k]) for k in range(1, 6) if k < len(eq)]
            self.portfolio.five_day_loss = sum(recent)
        else:
            self.portfolio.day_loss = 0.0
            self.portfolio.five_day_loss = 0.0

    def _range_snap(self, d: date) -> RegimeSnapshot:
        return RegimeSnapshot(asof=d, regime=Regime.RANGE, max_position=0.5,
                              min_score=0.0, min_percentile=0.5)

    def _trading_days(self, start: date, end: date) -> list[date]:
        idx = self.hub.get_index_bars("000300.SH", start, end)
        if idx is None or idx.empty:
            return []
        return sorted(pd.to_datetime(idx["date"]).dt.date.unique().tolist())

    def _universe(self, day: date) -> list[str]:
        try:
            infos = self.hub.get_instruments()
        except Exception:  # noqa: BLE001
            return []
        if isinstance(infos, dict):
            return list(infos.keys())
        return [getattr(i, "symbol", str(i)) for i in (infos or [])]

    def _instrument_map(self, syms: list[str]) -> dict:
        try:
            infos = self.hub.get_instruments(syms)
        except Exception:  # noqa: BLE001
            return {}
        if isinstance(infos, dict):
            return infos
        return {getattr(i, "symbol", ""): i for i in (infos or []) if getattr(i, "symbol", "")}

    # ------------------------------------------------------------ 行情内存索引
    def _hist_bars(self, symbol: str, adjust: Adjust) -> pd.DataFrame | None:
        """取标的 [fixed_start, 回测末] 全量日线子帧（内存索引优先，缺失回退 get_bars）。

        回测预热阶段已把全 universe 日线按 (标的, 复权) 建好内存索引，循环里所有
        取数都走这里，避免逐标的回源（重新落盘读盘 / 重下）。仅当索引里没有该标的
        （如 universe 中途变化）才回退 DataHub。
        """
        idx = self._bars_index.get(adjust)
        if idx is not None and symbol in idx:
            return idx[symbol]
        try:
            df = self.hub.get_bars(symbol, Freq.D1, self.fixed_start, self._bt_end, adjust)
        except DataUnavailableError:
            return None
        return df if (df is not None and not df.empty) else None

    def _lookup_bar(self, symbol: str, day: date, adjust: Adjust) -> Bar | None:
        """从内存索引定位 day 当天那根 bar（与旧 get_bars 语义一致：当日无 bar 即 None）。

        注意：这里用**精确等于 day** 而非"≤ day 的最后一根"——回测传入的 day 都是
        交易日（来自指数交易日历），但个股可能当日停牌（无数据）。旧实现
        ``get_bars(..., day)`` 取 [fixed_start, day] 后按 ``date == day`` 过滤，
        停牌日返回 None → 跳过该标的本次撮合。若改成"回落上一根"会给停牌票用陈旧价
        下错误订单（止损/买入），故必须保留精确匹配。
        """
        sub = self._hist_bars(symbol, adjust)
        if sub is None or sub.empty:
            return None
        sd = sub[sub["date"] == pd.Timestamp(day)]
        if sd.empty:
            return None
        row = sd.iloc[-1]
        return Bar(
            symbol=symbol, date=day,
            open=float(row.get("open", 0)), high=float(row.get("high", 0)),
            low=float(row.get("low", 0)), close=float(row.get("close", 0)),
            volume=float(row.get("volume", 0)), amount=float(row.get("amount", 0)),
            limit_up=row.get("limit_up"), limit_down=row.get("limit_down"),
        )

    def _bar(self, symbol: str, day: date, *, adjust: Adjust = Adjust.NONE) -> Bar | None:
        """取单日 bar。**默认不复权（NONE）**：撮合价必须与实盘执行语义一致。

        ``adjust`` 仅用于特殊判断场景（如 _gap_guard 用 HFQ 判断跳空以避开除权日假缺口）。
        自 2026-08-13 起直接走预热好的行情内存索引，不再逐标的回源取数。
        """
        try:
            return self._lookup_bar(symbol, day, adjust)
        except DataUnavailableError:
            # 个股停牌/数据缺口：跳过该标的本次撮合，不中断整场回测
            logger.warning("取数失败跳过 %s %s（停牌或数据缺失）", symbol, day)
            return None

    def _last_prices(self, day: date, *, adjust: Adjust = Adjust.NONE) -> dict[str, float]:
        """取持仓票收盘价用于市值标记。**默认不复权（NONE）**：与实盘组合估值口径一致。

        走行情内存索引，逐票切片收盘价（O(1) 定位），不再逐标的回源取数。
        """
        out: dict[str, float] = {}
        for sym in list(self.portfolio.positions):
            sub = self._hist_bars(sym, adjust)
            if sub is None or sub.empty:
                continue  # 停牌：沿用上一收盘价（不更新即沿用）
            sd = sub[sub["date"] <= pd.Timestamp(day)]
            if not sd.empty:
                out[sym] = float(sd.iloc[-1]["close"])
        return out
