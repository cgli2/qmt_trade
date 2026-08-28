"""个股存量持仓日内做T（高抛低吸）独立策略。

设计（2026-08-19，与 etf_t0 / tail_pick / strategylab 正交，绝不改动既有策略）
----------------------------------------------------------------------------
目标：对**已经持有**的个股（存量持仓）做日内 T+0 高抛低吸，赚取日内波动价差，
把持仓成本降下来；底仓股数日内不变、隔夜不动。**不新建仓、不净加仓、不净减仓**。

适用：已持仓、流动性好、日内有振幅（非一字板/非停牌）的股票。A 股 T+1 账本
下只能【先卖后买】：涨高卖 T 仓 → 跌回买回，当日买入份额当日不可卖，绝无先买后卖。

纪律（与主策略共享风控链，P3/P7 绝不绕过）：
- 只做 ``strategies.stock_t0.symbols`` 白名单里的**存量持仓**：实盘直接读
  ``ctx.portfolio.positions``，不在名单里或未持有 → 跳过；回测用
  ``initial_shares`` / ``base_fraction`` 构造存量底仓，底仓全程不动。
- 高抛信号：现价 ≥ 当日 VWAP×(1+sell_dev_threshold)，且 15 分钟动量未强涨
  （filter 逆势过滤，防"卖了继续涨"），且当日已现振幅落在
  [min_amplitude_pct, max_amplitude_pct]（有空间且不失控），且当日涨幅未超
  trend_guard_pct（强趋势日不做T，防卖飞）。
- 低吸回补：回归 VWAP（≤ close_leg_dev）或网格止盈（较开腿价回落 grid_step）。
- 单腿止损：卖出后反向上涨超 stop_pct → 立即止损回补（接受小亏，T仓绝不裸奔）。
- 尾盘强平：force_flat_time 起所有未回补 T 仓强制买回（当日 T 仓归零，底仓不变）。
- 单腿名义金额下限 min_leg_notional：小于该金额不开腿，避免小单被最低佣金吃光。
- 单日 T0 净亏损超 max_daily_loss_pct × 总资产 → 当日停开新腿。
- 每标的最多 max_trades_per_symbol_per_day 次开腿；开腿间隔 ≥ min_interval_minutes。
- 回测与实盘同口径：SimGateway（撮合）+ CostModel（成本）+ PortfolioState（记账）；
  实盘订单经 ctx.execution.submit_intent 走完整风控链（OrderGuard → Gate-1 →
  KillSwitch → Gateway → 记账）。

实盘 v1 已知限制（诚实声明）
- 卖出腿用 REDUCE+reduce_shares 精确控量；买回腿用新增的 exact_buy_shares
  精确回补（绕过共享 sizer，底仓不变）。若 Gate-1 因持仓数/Regime 仓位上限
  拦截买回（如满仓 8 只），则当日底仓净减（等效减仓，绝不无控制加仓），
  由盘后 sold_qty vs bought_qty 对账记录，次日可按原底仓继续做T。
- 部分成交/挂单超时等实时细节 v1 从简：以 fill.quantity 记账，残余敞口由尾盘
  强平与次日对账兜底。实盘前建议先 paper 观察期跑 2~4 周再上 live。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Any

import pandas as pd

from ..backtest.metrics import performance
from ..core.trading import Fill, Order, OrderType, Side
from ..datahub.types import Adjust, Bar, Freq
from ..execution.costs import CostModel
from ..execution.gateway.simulator import SimGateway
from ..portfolio.state import PortfolioState
from ..strategies.base import StrategyConfig, StrategyResult, load_config
from ..strategies.tail_pick import build_cost_attribution, build_gap_attribution

logger = logging.getLogger(__name__)

#: 个股做T 调度任务名（与 scheduler/runner.py 的 JobSpec.name 一致）
JOB_NAME = "stock_t0_intraday"


def _parse_t(text: str) -> dtime:
    """'09:35' → datetime.time(9, 35)。解析失败返回 00:00（保守：视为最早可交易）。"""
    try:
        h, m = str(text).split(":")
        return dtime(int(h), int(m))
    except Exception:                                # noqa: BLE001
        return dtime(0, 0)


def mom_sell_ok(cfg: "StockT0Config", mom: float, sym: str | None = None) -> bool:
    """卖高腿的动量门槛（off/filter/confirm 三模式）。回测与实盘共用（P7）。

    - off:     恒 True（纯卖高买低网格）
    - filter:  逆势过滤，未强涨才卖高（mom < threshold）——防"卖了继续涨"
    - confirm: 顺势确认，强涨才卖高（mom >= threshold）——趋势延续假设
    """
    mode, _win, thr = cfg.momentum_params_for(sym or "")
    if mode == "confirm":
        return mom >= thr
    if mode == "filter":
        return mom < thr
    return True


@dataclass
class StockT0Config(StrategyConfig):
    """个股做T 全部参数，从 ``config/settings.yaml::strategies.stock_t0`` 加载。"""

    #: 存量持仓做T白名单（只对已持有且在此名单的标的做T）；空列表 = 不做T
    symbols: list[str] = field(default_factory=list)
    #: 回测用初始持仓：{symbol: 股数}；缺省标的按 base_fraction 兜底建底仓
    initial_shares: dict[str, int] = field(default_factory=dict)
    #: 回测兜底：无 initial_shares 的标的自带底仓占初始资金比例
    base_fraction: float = 0.10
    #: 每次做T股数 = 底仓股数 × 该比例（整百；不足一手取一手；绝不清仓，至少留一手）
    t_slice_ratio: float = 0.30
    #: 单腿名义金额下限（元）：低于则放弃开腿（防最低佣金吃掉利润）
    min_leg_notional: float = 10_000.0
    #: 开腿信号：现价高于当日 VWAP 该比例 → 卖出做T
    sell_dev_threshold: float = 0.008
    #: 平腿信号1：偏离回归到该比例内（回 VWAP 附近买回）
    close_leg_dev: float = 0.002
    #: 平腿信号2：相对开腿价回落该比例（网格止盈买回）
    grid_step: float = 0.005
    #: 单腿止损：卖出后反向上涨该比例 → 止损回补
    stop_pct: float = 0.005
    #: 振幅护栏：已现振幅 < min_amplitude_pct 不做T（无空间）；> max_amplitude_pct 不做T
    min_amplitude_pct: float = 0.012
    max_amplitude_pct: float = 0.05
    #: 当日涨幅护栏：现价较昨收已涨超该比例 → 不再卖高（强趋势日卖了买不回来）
    trend_guard_pct: float = 0.035
    #: 单日每标的最多开腿次数
    max_trades_per_symbol_per_day: int = 2
    #: 开腿最小间隔（分钟）
    min_interval_minutes: int = 5
    #: 开腿窗口
    open_t_start: str = "09:35"
    open_t_end: str = "14:30"
    #: 尾盘强平时刻（当日 T 仓归零）
    force_flat_time: str = "14:50"
    #: 单日 T0 净亏损上限（占总资产比例），触发当日停开新腿
    max_daily_loss_pct: float = 0.002
    #: 当日分钟线不足该根数不参与做T（数据质量护栏）
    min_minutes_per_day: int = 5
    #: 日内动量模式（VWAP 回归增强）：off / filter（逆势过滤）/ confirm（顺势确认）
    momentum_mode: str = "filter"
    #: 动量回看窗口（分钟）
    momentum_window_min: int = 15
    #: 动量阈值：窗口涨跌幅超此比例视为强趋势（filter 禁止卖高 / confirm 要求卖高）
    momentum_threshold: float = 0.004
    #: 按标的动量参数覆盖（{symbol: {momentum_mode, momentum_window_min, momentum_threshold}}）
    momentum_override: dict[str, dict] = field(default_factory=dict)

    def momentum_params_for(self, sym: str) -> tuple[str, int, float]:
        """返回某标的生效的 (momentum_mode, momentum_window_min, momentum_threshold)。

        优先级：momentum_override[sym] > 全局字段。回测与实盘共用（P7）。
        """
        ov = (self.momentum_override or {}).get(sym) or {}
        return (
            str(ov.get("momentum_mode", self.momentum_mode) or "off").lower(),
            int(ov.get("momentum_window_min", self.momentum_window_min) or 0),
            float(ov.get("momentum_threshold", self.momentum_threshold) or 0.0),
        )


# ============================================================================ 回测
class StockT0Backtester:
    """个股存量持仓做T 分钟级回测器（自包含，复用 SimGateway/CostModel/PortfolioState）。

    与 ETF T+0 的区别：**不建底仓** —— 存量持仓由 ``initial_shares``（或
    base_fraction 兜底）在首日构造，全程不动；只对底仓做日内先卖后买，
    尾盘 T 仓归零，底仓股数不变、成本下降。
    """

    sid = "stock_t0"
    config_class = StockT0Config

    def __init__(self, settings, hub, *, initial_cash: float = 1_000_000.0,
                 config: StockT0Config | None = None):
        self.settings = settings
        self.hub = hub
        self.initial_cash = initial_cash
        self.config = config or self.config_class()
        self.cost = CostModel.from_settings(settings)
        self.gateway = SimGateway()
        self.portfolio = PortfolioState(cash=initial_cash)
        self.fills: list[Fill] = []
        self.details: list[str] = []
        self._minute_cache: dict[tuple[str, date], pd.DataFrame | None] = {}
        self._day_cache: dict[tuple[str, date], dict | None] = {}
        self._seq = 0
        self._minute_available = False

    # ------------------------------------------------------------ 主入口
    def run(self, start: date, end: date) -> StrategyResult:
        cfg = self.config
        universe = [s for s in (cfg.symbols or []) if s]
        if not universe:
            return StrategyResult(details=["未配置做T标的（strategies.stock_t0.symbols）"])
        days = self._trading_days(start, end)
        if len(days) < 3:
            return StrategyResult(details=["交易日不足"])
        probe = self._minute_bars(universe[0], days[0])
        self._minute_available = probe is not None and len(probe) > 1
        logger.info("个股做T 回测 %s~%s 标的=%s 分钟线=%s", start, end, universe,
                    "可用" if self._minute_available else "不可用（仅持底仓，无做T）")

        result = StrategyResult()
        t0 = {"legs": 0, "closed": 0, "wins": 0, "losses": 0, "pnl": 0.0,
              "days_traded": 0, "day_pnl": []}
        base_shares_map = self._enter_base(days[0], universe)   # 存量底仓，仅首日一次
        for i, d in enumerate(days):
            if i + 1 < len(days):
                self.portfolio.mark_t1(days[i + 1])
            day_pnl = self._intraday_t0(d, universe, t0, base_shares_map)
            if abs(day_pnl) > 1e-9:
                t0["days_traded"] += 1
            t0["pnl"] += day_pnl
            t0["day_pnl"].append({"date": d.isoformat(), "pnl": round(day_pnl, 2)})
            last = self._last_prices(d)
            self.portfolio.refresh(last)
            self.portfolio.record_equity(day_end=True)
            result.equity_curve.append(round(self.portfolio.total_asset, 2))

        result.trades = list(self.fills)
        result.closed_trades = list(self.portfolio.closed_trades)
        result.open_positions = [
            {"symbol": s, "avg_cost": round(p.avg_cost, 4),
             "last_price": round(p.last_price or p.avg_cost, 4), "shares": p.shares,
             "opened_at": p.opened_at.isoformat() if p.opened_at else None}
            for s, p in self.portfolio.positions.items()
        ]
        result.metrics = performance(
            result.equity_curve, trades=result.trades,
            realized_log=self.portfolio.realized_log)
        result.cost_attribution = build_cost_attribution(
            self.cost, result.trades, result.closed_trades, self.initial_cash)
        result.gap_attribution = build_gap_attribution(self.cost, result.closed_trades)
        if result.metrics:
            result.metrics["t0_pnl"] = round(t0["pnl"], 2)
            result.metrics["t0_legs"] = t0["legs"]
            result.metrics["t0_win_legs"] = t0["wins"]
            result.metrics["t0_loss_legs"] = t0["losses"]
            result.metrics["t0_days_traded"] = t0["days_traded"]
            result.metrics["minute_available"] = self._minute_available
        self.details = [
            f"个股做T 回测：标的={universe}，分钟线={'可用' if self._minute_available else '不可用'}",
            f"做T腿数={t0['legs']}，平腿={t0['closed']}，胜腿={t0['wins']}，亏腿={t0['losses']}，"
            f"T0净盈亏={t0['pnl']:.2f}，有T交易日={t0['days_traded']}",
        ]
        result.details = list(self.details)
        result.minute_available = self._minute_available
        return result

    # ------------------------------------------------------------ 存量底仓
    def _enter_base(self, day: date, universe: list[str]) -> dict[str, int]:
        """首日按 initial_shares（或 base_fraction 兜底）构造存量底仓。

        底仓视为昨日已持有（opened_at 设为回测开始前），首日即可卖（can_use=shares）。
        返回 {symbol: 底仓股数}，供每日做T切片用（底仓数量全程不变）。
        """
        cfg = self.config
        from datetime import timedelta
        from ..core.trading import Position
        prev = day - timedelta(days=1)
        base_map: dict[str, int] = {}
        for sym in universe:
            bar = self._bar(sym, day)
            if bar is None:
                continue
            px = float(bar.get("open") or 0)
            if px <= 0:
                continue
            shares = int((cfg.initial_shares or {}).get(sym) or 0)
            if shares <= 0:
                notional = min(self.initial_cash * cfg.base_fraction,
                               self.portfolio.cash * 0.98)
                if notional <= 0:
                    continue
                shares = int(notional / px // 100 * 100)
            if shares < 200:                       # 至少留一手做T + 底仓，小于200股放弃
                continue
            cost = float(px)
            add_value = shares * cost
            if self.portfolio.cash < add_value:
                continue
            self.portfolio.positions[sym] = Position(
                symbol=sym, shares=shares, avg_cost=cost, can_use=shares,
                industry="", opened_at=prev, highest_since_open=cost)
            self.portfolio.cash -= add_value
            base_map[sym] = shares
        return base_map

    # ------------------------------------------------------------ 日内回转
    def _intraday_t0(self, day: date, universe: list[str], t0: dict,
                     base_map: dict[str, int]) -> float:
        """对每个标的跑当日分钟级先卖后买 T0，返回当日 T0 净盈亏（含费用）。"""
        cfg = self.config
        open_start, open_end = _parse_t(cfg.open_t_start), _parse_t(cfg.open_t_end)
        force_flat = _parse_t(cfg.force_flat_time)
        day_pnl = 0.0
        day_blocked = False
        for sym in universe:
            base_shares = int(base_map.get(sym) or 0)
            if base_shares < 200:
                continue
            minute = self._minute_bars(sym, day)
            if minute is None or len(minute) < cfg.min_minutes_per_day:
                continue
            pos = self.portfolio.positions.get(sym)
            if pos is None or pos.shares <= 0:
                continue
            slice_qty = self._slice_qty(base_shares, cfg)
            if slice_qty <= 0 or slice_qty >= base_shares:
                continue
            day_bar = self._bar(sym, day)
            prev_close = float((day_bar or {}).get("prev_close") or 0) or float(
                minute.iloc[0]["open"] or 0)

            bars = minute.reset_index(drop=True)
            closes = bars["close"].astype(float).to_numpy()
            highs = bars["high"].astype(float).to_numpy()
            lows = bars["low"].astype(float).to_numpy()
            vols = bars["volume"].astype(float).to_numpy()
            times = bars["time"].tolist()
            cum_vol = pd.Series(vols).cumsum()
            cum_pv = pd.Series(closes * vols).cumsum()
            vwap = (cum_pv / cum_vol.replace(0, float("nan"))).to_numpy()
            # 已现振幅（截至当前分钟的日内高-低），做T空间护栏
            run_high = pd.Series(highs).cummax().to_numpy()
            run_low = pd.Series(lows).cummin().to_numpy()
            amp = (run_high - run_low) / prev_close if prev_close > 0 else 0.0

            legs: list[dict] = []
            sold_qty = 0
            opened_today = 0
            last_leg_idx: int | None = None
            _m_mode, mom_window, _m_thr = cfg.momentum_params_for(sym)

            for idx in range(len(bars)):
                t = times[idx]
                px = float(closes[idx])
                vw = float(vwap[idx])
                dv = (px / vw - 1.0) if (not math.isnan(vw) and vw > 0) else 0.0
                day_ret = (px / prev_close - 1.0) if prev_close > 0 else 0.0
                # 窗口动量：过去 mom_window 分钟涨跌幅（不足窗口用首根近似）
                mom = 0.0
                if mom_window > 0:
                    ref_i = max(0, idx - mom_window)
                    ref_px = float(closes[ref_i])
                    mom = (px / ref_px - 1.0) if ref_px > 0 else 0.0
                bar = self._bar_from_row(bars.iloc[idx], sym, day)

                # ---- 1) 平腿优先（止损 → 网格/回归）----
                for leg in list(legs):
                    if self._try_close_leg(sym, leg, px, dv, bar, day, cfg, t0, False):
                        day_pnl += float(leg["pnl"])
                        legs.remove(leg)

                # ---- 2) 开新腿（窗口内、无开腿、未超限、护栏全过、未触发日亏熔断）----
                if (not day_blocked and not legs
                        and opened_today < cfg.max_trades_per_symbol_per_day
                        and open_start <= t <= open_end
                        and cfg.min_amplitude_pct <= amp[idx] <= cfg.max_amplitude_pct):
                    if last_leg_idx is not None and \
                            (idx - last_leg_idx) < cfg.min_interval_minutes:
                        pass                        # 间隔不足，跳过本分钟
                    elif (dv >= cfg.sell_dev_threshold
                          and day_ret <= cfg.trend_guard_pct          # 强趋势日不卖高
                          and mom_sell_ok(cfg, mom, sym)              # 动量模式
                          and sold_qty + slice_qty <= base_shares - 100
                          and slice_qty * px >= cfg.min_leg_notional):  # 名义金额护栏
                        if self._fill(sym, "SELL", slice_qty, px, bar, day, "STOCK_T0_SELL"):
                            legs.append({"side": "SELL", "qty": slice_qty,
                                         "price": float(self.fills[-1].price),
                                         "pnl": 0.0, "closed": False})
                            sold_qty += slice_qty
                            opened_today += 1
                            last_leg_idx = idx
                            t0["legs"] += 1

                # ---- 3) 尾盘强平（当日 T 仓归零）----
                if t >= force_flat and legs:
                    for leg in list(legs):
                        if self._try_close_leg(sym, leg, px, dv, bar, day, cfg, t0, True):
                            day_pnl += float(leg["pnl"])
                    legs = [l for l in legs if not self._leg_closed(l)]
                    if legs:
                        # 强平失败（如数据异常）——按现价市价兜底再试一次
                        for leg in list(legs):
                            self._fill(sym, "BUY", int(leg["qty"]), px, bar, day,
                                       "STOCK_T0_FORCE_FLAT")
                            if self.fills:
                                buy_fill = self.fills[-1]
                                if buy_fill.symbol == sym and buy_fill.side is Side.BUY:
                                    leg_px = float(leg["price"])
                                    qty = int(leg["qty"])
                                    leg["pnl"] = (leg_px - float(buy_fill.price)) * qty \
                                        - float(buy_fill.total_fee or 0.0)
                                    leg["closed"] = True
                                    day_pnl += leg["pnl"]
                                    self._settle_leg(t0, leg, leg["pnl"] >= 0)
                        legs.clear()

                if day_pnl <= -cfg.max_daily_loss_pct * self.initial_cash:
                    day_blocked = True

            # ---- 兜底：最后一根仍未平的腿按收盘价强平 ----
            if legs and len(bars):
                px = float(closes[-1])
                bar = self._bar_from_row(bars.iloc[-1], sym, day)
                for leg in list(legs):
                    if self._try_close_leg(sym, leg, px, 0.0, bar, day, cfg, t0, True):
                        day_pnl += float(leg["pnl"])
                legs = [l for l in legs if not self._leg_closed(l)]
                for leg in list(legs):
                    self._fill(sym, "BUY", int(leg["qty"]), px, bar, day,
                               "STOCK_T0_FORCE_FLAT")
                    if self.fills:
                        buy_fill = self.fills[-1]
                        if buy_fill.symbol == sym and buy_fill.side is Side.BUY:
                            leg_px = float(leg["price"])
                            qty = int(leg["qty"])
                            leg["pnl"] = (leg_px - float(buy_fill.price)) * qty \
                                - float(buy_fill.total_fee or 0.0)
                            leg["closed"] = True
                            day_pnl += leg["pnl"]
                            self._settle_leg(t0, leg, leg["pnl"] >= 0)
                legs.clear()
        return day_pnl

    # ------------------------------------------------------------ 平腿
    @staticmethod
    def _leg_closed(leg: dict) -> bool:
        return bool(leg.get("closed", False))

    def _try_close_leg(self, sym: str, leg: dict, px: float, dv: float,
                       bar: Bar, day: date, cfg: StockT0Config, t0: dict,
                       force: bool) -> bool:
        """尝试平一条 SELL 腿（买回）。成功返回 True 并结算 pnl。"""
        if leg.get("closed"):
            return False
        leg_px = float(leg["price"])
        qty = int(leg["qty"])
        buyback_px: float | None = None
        if force:
            buyback_px = px
            signal = "STOCK_T0_FORCE_FLAT"
        elif px >= leg_px * (1 + cfg.stop_pct):
            buyback_px = leg_px * (1 + cfg.stop_pct)   # 反向止损：接受小亏
            signal = "STOCK_T0_STOP_BUYBACK"
        elif px <= leg_px * (1 - cfg.grid_step) or dv <= cfg.close_leg_dev:
            buyback_px = px                             # 网格止盈 / 回归VWAP
            signal = "STOCK_T0_BUYBACK"
        else:
            return False
        fill = self._fill(sym, "BUY", qty, buyback_px, bar, day, signal)
        if fill is None:
            return False
        pnl = (leg_px - float(fill.price)) * qty - float(fill.total_fee or 0.0)
        leg["pnl"] = pnl
        leg["closed"] = True
        self._settle_leg(t0, leg, pnl >= 0)
        return True

    def _settle_leg(self, t0: dict, leg: dict, win: bool) -> None:
        t0["closed"] += 1
        if win:
            t0["wins"] += 1
        else:
            t0["losses"] += 1

    @staticmethod
    def _slice_qty(base_shares: int, cfg: StockT0Config) -> int:
        # 底仓不足 200 股（一手做T + 一手底仓）不做T
        if base_shares < 200:
            return 0
        raw = base_shares * cfg.t_slice_ratio
        qty = int(raw // 100 * 100)
        if qty < 100:
            qty = 100                      # 不足一手补一手（有底仓空间时）
        # 绝不清仓：至少留 100 股底仓
        return min(qty, base_shares - 100)

    # ------------------------------------------------------------ 撮合与行情
    def _fill(self, sym: str, side: str, qty: int, ref_price: float,
              bar: Bar | None, day: date, signal: str) -> Fill | None:
        side_enum = Side.BUY if side == "BUY" else Side.SELL
        if side_enum is Side.SELL:
            pos = self.portfolio.positions.get(sym)
            if pos is None or pos.shares < qty:
                return None
            # A股 T+1 纪律：只允许卖「可卖数量」（隔夜底仓 + 昨日买入）
            if pos.can_use < qty:
                return None
        if bar is None or ref_price <= 0:
            return None
        self._seq += 1
        order = Order(order_id=f"stk0_{day.isoformat()}_{self._seq}_{sym}",
                      symbol=sym, side=side_enum, quantity=int(qty),
                      price=round(ref_price, 4), order_type=OrderType.LIMIT)
        fill = self.gateway.submit(order, day, bar, self.cost)
        if fill is None:
            return None
        self.portfolio.apply_fill(fill, fill.total_fee, is_buy=(side_enum is Side.BUY),
                                  signal=signal, asof=day)
        if side_enum is Side.BUY:
            # T+1：当日买入份额不可卖（can_use 不增加），由 mark_t1 次日解冻
            pos = self.portfolio.positions.get(sym)
            if pos is not None and pos.opened_at is None:
                pos.opened_at = day
        self.fills.append(fill)
        return fill

    def _minute_bars(self, sym: str, day: date) -> pd.DataFrame | None:
        key = (sym, day)
        if key in self._minute_cache:
            return self._minute_cache[key]
        df = None
        try:
            df = self.hub.get_bars([sym], Freq.M1, day, day, Adjust.NONE, validate=True)
        except Exception:                              # noqa: BLE001
            df = None
        if df is None or df.empty:
            self._minute_cache[key] = None
            return None
        df = df.copy()
        df["dt"] = pd.to_datetime(df["date"])
        df = df[df["dt"].dt.date == day].sort_values("dt").reset_index(drop=True)
        df["time"] = df["dt"].dt.time
        self._minute_cache[key] = df if not df.empty else None
        return self._minute_cache[key]

    @staticmethod
    def _bar_from_row(row, sym: str, day: date) -> Bar:
        return Bar(symbol=sym, date=day,
                   open=float(row["open"] or 0), high=float(row["high"] or 0),
                   low=float(row["low"] or 0), close=float(row["close"] or 0),
                   volume=float(row["volume"] or 0),
                   amount=float(row.get("amount", 0) or 0))

    @staticmethod
    def _to_bar(bar: dict) -> Bar:
        return Bar(symbol=bar.get("symbol") or "", date=bar.get("date"),
                   open=float(bar.get("open") or 0), high=float(bar.get("high") or 0),
                   low=float(bar.get("low") or 0), close=float(bar.get("close") or 0),
                   volume=float(bar.get("volume") or 0),
                   amount=float(bar.get("amount") or 0),
                   prev_close=float(bar.get("prev_close") or 0),
                   limit_up=bar.get("limit_up"), limit_down=bar.get("limit_down"))

    # ------------------------------------------------------------ 工具
    def _trading_days(self, start: date, end: date) -> list[date]:
        try:
            idx = self.hub.get_index_bars("000300.SH", start, end)
        except Exception:                              # noqa: BLE001
            return []
        if idx is None or idx.empty:
            return []
        return sorted(pd.to_datetime(idx["date"]).dt.date.unique().tolist())

    def _bar(self, sym: str, day: date) -> dict | None:
        """当日日线 bar dict（prev_close/振幅参考）。"""
        key = (sym, day)
        if key in self._day_cache:
            return self._day_cache[key]
        try:
            df = self.hub.get_bars([sym], Freq.D1, day, day, Adjust.NONE, validate=True)
        except Exception:                              # noqa: BLE001
            return None
        if df is None or df.empty:
            self._day_cache[key] = None
            return None
        row = df.iloc[-1]
        out = {c: row.get(c) for c in ("open", "high", "low", "close", "volume",
                                       "amount", "prev_close", "limit_up",
                                       "limit_down", "is_suspended")}
        out["symbol"] = sym
        out["date"] = day
        self._day_cache[key] = out
        return out

    def _last_prices(self, day: date) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym in list(self.portfolio.positions):
            bar = self._bar(sym, day)
            if bar:
                try:
                    out[sym] = float(bar["close"])
                except (TypeError, ValueError):
                    pass
        return out


# ============================================================================ 实盘
class StockT0LiveRunner:
    """个股存量持仓做T 实盘盘中管理器（由调度任务 ``stock_t0_intraday`` 周期性调用）。

    独立于主策略 intraday / ETF T+0 / 策略实验室：只对 ``strategies.stock_t0.symbols``
    白名单里的**已有持仓**做日内先卖后买；不建仓、不净加仓。启停由
    ``strategies.stock_t0.enabled`` 控制（WebUI「策略实验室」开关即启停）。

    风控：所有订单经 ``ctx.execution.submit_intent``（OrderGuard → Gate-1 →
    KillSwitch → Gateway → 记账），与主策略同一执行链（P3/P7）。
    卖出腿 REDUCE+reduce_shares 精确控量；买回腿 exact_buy_shares 精确回补，
    底仓股数保持不变（Gate-1 拦截时底仓净减，绝不无控制加仓）。
    """

    def __init__(self, jr):
        self.jr = jr
        self.ctx = jr.ctx
        self._regime = None
        self._seq = 0
        self._prev_close_cache: dict[str, float] = {}

    # ------------------------------------------------------------ 配置/状态
    def _cfg(self) -> StockT0Config:
        return load_config(self.ctx.settings, StockT0Config, "stock_t0")

    def _state_key(self) -> str:
        return f"stock_t0:state:{self.jr.today.isoformat()}"

    def _load_state(self) -> dict:
        try:
            raw = self.ctx.shared_repos.system.get(self._state_key())
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except Exception:                              # noqa: BLE001
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self.ctx.shared_repos.system.set(
                self._state_key(), json.dumps(state, ensure_ascii=False, default=str),
                reason=f"个股做T 盘中状态 {self.jr.today}")
        except Exception as exc:                       # noqa: BLE001
            logger.warning("个股做T 状态落库失败: %s", exc)

    # ------------------------------------------------------------ 行情
    def _minute_bars(self, sym: str) -> pd.DataFrame | None:
        try:
            df = self.ctx.hub.get_bars([sym], Freq.M1, self.jr.today, self.jr.today,
                                       Adjust.NONE, validate=True)
        except Exception:                              # noqa: BLE001
            return None
        if df is None or df.empty:
            return None
        df = df.copy()
        df["dt"] = pd.to_datetime(df["date"])
        df = df.sort_values("dt").reset_index(drop=True)
        df["time"] = df["dt"].dt.time
        return df

    def _prev_close(self, sym: str) -> float:
        """昨收（T-1 收盘价）。盘中 T 日日线未生成，get_bars(D1, today, today)
        拿不到 prev_close → 振幅护栏恒为 0，开腿永不满足。改为取最近一个
        < today 的已收盘交易日收盘价（盘中盘后都稳定）。"""
        if sym in self._prev_close_cache:
            return self._prev_close_cache[sym]
        pc = 0.0
        try:
            from datetime import timedelta
            df = self.ctx.hub.get_bars(
                [sym], Freq.D1, self.jr.today - timedelta(days=15), self.jr.today,
                Adjust.NONE, validate=False)
            if df is not None and not df.empty:
                df = df.copy()
                df["dt"] = pd.to_datetime(df["date"])
                hist = df[df["dt"].dt.date < self.jr.today]
                if not hist.empty:
                    pc = float(hist.iloc[-1]["close"])
                else:
                    # 兜底：当日日线已生成（盘后）则用其 prev_close
                    row = df.iloc[-1]
                    pc = float(row.get("prev_close") or row["close"] or 0)
        except Exception:                              # noqa: BLE001
            pass
        self._prev_close_cache[sym] = pc
        return pc

    def _regime_snapshot(self):
        if self._regime is not None:
            return self._regime
        try:
            self._regime = self.ctx.pipeline.detector.detect(self.jr.today)
        except Exception:                              # noqa: BLE001
            from ..features.regime import Regime, RegimeSnapshot
            self._regime = RegimeSnapshot(
                asof=self.jr.today, regime=Regime.RANGE, max_position=0.5,
                min_score=0.0, min_percentile=0.7)
        return self._regime

    def _instrument(self, sym: str):
        try:
            return self.ctx.hub.get_instrument(sym)
        except Exception:                              # noqa: BLE001
            return None

    # ------------------------------------------------------------ 下单
    def _intent(self, sym: str, action: str, price: float) -> Any:
        from ..brain.schemas import TradeIntent
        return TradeIntent(
            symbol=sym, action=action, confidence=0.9,
            conviction="HIGH" if action in ("SELL", "REDUCE") else "MEDIUM",
            entry_type="LIMIT", entry_ref_price=round(price, 4),
            stop_loss_type="FIXED_PCT", stop_loss_value=0.005,
            risk_budget_hint=0.3, max_weight_hint=0.03,
            time_horizon_days=5, max_holding_days=20, valid_until=self.jr.today,
            reasoning=f"个股存量持仓做T [{sym}]")

    def _make_bar(self, sym: str, price: float) -> Bar | None:
        bars = self._minute_bars(sym)
        if bars is not None and not bars.empty:
            row = bars.iloc[-1]
            return Bar(symbol=sym, date=self.jr.today,
                       open=float(row["open"] or price), high=float(row["high"] or price),
                       low=float(row["low"] or price), close=float(row["close"] or price),
                       volume=float(row["volume"] or 0),
                       amount=float(row.get("amount", 0) or 0))
        if price > 0:
            return Bar(symbol=sym, date=self.jr.today,
                       open=price, high=price, low=price, close=price,
                       volume=0.0, amount=0.0)
        return None

    def _submit(self, sym: str, action: str, price: float, *,
                reduce_shares: int = 0, exact_buy_shares: int = 0,
                signal: str = "STOCK_T0") -> Any:
        bar = self._make_bar(sym, price)
        if bar is None:
            return None
        it = self._intent(sym, action, price)
        self._seq += 1
        try:
            return self.ctx.execution.submit_intent(
                it, bar=bar, market_day=self.jr.today, asof=self.jr.today,
                regime=self._regime_snapshot(), instrument=self._instrument(sym),
                sym_industry={sym: ""},
                plan_id=f"stk0_{self.jr.today:%Y%m%d}", seq=self._seq,
                signal=signal, reduce_shares=reduce_shares,
                exact_buy_shares=exact_buy_shares)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("个股做T %s %s %s 下单异常: %s",
                           self.jr.today, action, sym, exc)
            return None

    # ------------------------------------------------------------ 盘中巡检
    def tick(self) -> dict:
        cfg = self._cfg()
        if not cfg.enabled:
            return {"skipped": True, "reason": "enabled=false（UI 停用）"}
        symbols = [s for s in (cfg.symbols or []) if s]
        if not symbols:
            return {"skipped": True, "reason": "未配置做T标的（strategies.stock_t0.symbols）"}
        if not self.ctx.calendar.is_trading_day(self.jr.today):
            return {"skipped": True, "reason": "非交易日"}
        now = datetime.now()
        session = self.ctx.calendar.session_of(now)
        if not session.is_continuous:
            return {"skipped": True, "reason": f"非连续竞价时段（{session.value}）"}

        open_start, open_end = _parse_t(cfg.open_t_start), _parse_t(cfg.open_t_end)
        force_flat = _parse_t(cfg.force_flat_time)
        state = self._load_state()
        summary = {"skipped": False, "symbols": 0, "legs_opened": 0,
                   "legs_closed": 0, "fills": 0, "rejected": 0,
                   "forced_flat": False, "base": 0}

        for sym in symbols:
            pos = self.ctx.portfolio.positions.get(sym)
            if pos is None or pos.shares < 200:
                continue                                # 未持有 / 底仓太小 → 不做T
            base_shares = int(pos.shares)
            bars = self._minute_bars(sym)
            if bars is None or len(bars) < cfg.min_minutes_per_day:
                continue
            closes = bars["close"].astype(float).to_numpy()
            highs = bars["high"].astype(float).to_numpy()
            lows = bars["low"].astype(float).to_numpy()
            vols = bars["volume"].astype(float).to_numpy()
            cum_vol = pd.Series(vols).cumsum()
            cum_pv = pd.Series(closes * vols).cumsum()
            vwap = float((cum_pv / cum_vol.replace(0, float("nan"))).iloc[-1])
            price = float(closes[-1])
            tv = bars.iloc[-1]["time"]
            dv = (price / vwap - 1.0) if (not math.isnan(vwap) and vwap > 0) else 0.0
            prev_close = self._prev_close(sym)
            day_ret = (price / prev_close - 1.0) if prev_close > 0 else 0.0
            amp = ((float(highs.max()) - float(lows.min())) / prev_close
                   if prev_close > 0 else 0.0)
            _m_mode, mom_window, _m_thr = cfg.momentum_params_for(sym)
            mom = 0.0
            if mom_window > 0:
                ref_i = max(0, len(closes) - 1 - mom_window)
                ref_px = float(closes[ref_i])
                mom = (price / ref_px - 1.0) if ref_px > 0 else 0.0
            summary["symbols"] += 1

            st = state.setdefault(sym, {"sold_qty": 0, "bought_qty": 0,
                                        "legs": [], "day_pnl": 0.0,
                                        "blocked": False, "trades_today": 0,
                                        "last_trade_time": "", "base_shares": base_shares})
            total_asset = max(self.ctx.portfolio.total_asset, 1.0)
            if st.get("day_pnl", 0.0) <= -cfg.max_daily_loss_pct * total_asset:
                st["blocked"] = True

            # ---- 1) 平腿（止损 → 网格/回归）----
            for leg in list(st.get("legs", [])):
                if self._close_leg(sym, leg, price, dv, cfg, st):
                    summary["legs_closed"] += 1
                    summary["fills"] += 1
            # ---- 2) 尾盘强平 + 一次性买回未回补部分 ----
            if tv >= force_flat:
                for leg in list(st.get("legs", [])):
                    if self._close_leg(sym, leg, price, dv, cfg, st, force=True):
                        summary["legs_closed"] += 1
                        summary["fills"] += 1
                if st.get("legs"):
                    summary["forced_flat"] = True
                remain = max(0, st.get("sold_qty", 0) - st.get("bought_qty", 0))
                if remain > 0:
                    if self._buyback_remain(sym, price, remain, st):
                        summary["fills"] += 1
                    else:
                        summary["rejected"] += 1
            # ---- 3) 开新腿（实盘仅先卖后买）----
            elif (not st.get("blocked") and not st.get("legs")
                  and st.get("trades_today", 0) < cfg.max_trades_per_symbol_per_day
                  and open_start <= tv <= open_end):
                last_ts = st.get("last_trade_time") or ""
                if not self._interval_ok(last_ts, tv, cfg.min_interval_minutes):
                    continue
                opened = self._open_leg(sym, price, dv, mom, amp, day_ret, st, cfg)
                if opened == "opened":
                    summary["legs_opened"] += 1
                    summary["fills"] += 1
                elif opened == "rejected":
                    summary["rejected"] += 1
        self._save_state(state)
        return summary

    # ------------------------------------------------------------ 腿操作
    def _open_leg(self, sym: str, price: float, dv: float, mom: float, amp: float,
                  day_ret: float, st: dict, cfg: StockT0Config) -> str:
        pos = self.ctx.portfolio.positions.get(sym)
        base_shares = int(pos.shares) if pos else 0
        slice_qty = self._slice_qty(base_shares, cfg) if base_shares >= 200 else 0
        if slice_qty <= 0:
            return "skipped"
        # 振幅护栏 + 当日涨幅护栏 + 名义金额护栏
        if not (cfg.min_amplitude_pct <= amp <= cfg.max_amplitude_pct):
            return "skipped"
        if day_ret > cfg.trend_guard_pct:
            return "skipped"
        if slice_qty * price < cfg.min_leg_notional:
            return "skipped"
        # 实盘只做先卖后买（A股 T+1：当日买入不可卖，先买后卖无法平腿）
        if dv >= cfg.sell_dev_threshold:
            if not mom_sell_ok(cfg, mom, sym):
                return "skipped"
            if pos is None or pos.can_use < slice_qty:
                return "rejected"
            # 上一卖出腿尚未回补时不叠开（防累计净空）
            if st.get("sold_qty", 0) - st.get("bought_qty", 0) >= slice_qty:
                return "skipped"
            res = self._submit(sym, "REDUCE", price, reduce_shares=slice_qty,
                               signal="STOCK_T0_SELL")
            if res is None or not res.ok or res.fill is None:
                return "rejected"
            qty = int(res.fill.quantity)
            st["sold_qty"] = st.get("sold_qty", 0) + qty
            st.setdefault("legs", []).append(
                {"side": "SELL", "qty": qty, "price": float(res.fill.price),
                 "opened_at": datetime.now().strftime("%H:%M")})
            st["trades_today"] = st.get("trades_today", 0) + 1
            st["last_trade_time"] = datetime.now().strftime("%H:%M")
            return "opened"
        return "skipped"

    def _close_leg(self, sym: str, leg: dict, price: float, dv: float,
                   cfg: StockT0Config, st: dict, *, force: bool = False) -> bool:
        leg_px = float(leg["price"])
        qty = int(leg.get("qty") or 0)
        if qty <= 0:
            st.setdefault("legs", []).remove(leg)
            return False
        if force:
            res = self._submit(sym, "BUY", price, exact_buy_shares=qty,
                               signal="STOCK_T0_FORCE_FLAT")
        elif price >= leg_px * (1 + cfg.stop_pct):
            res = self._submit(sym, "BUY", leg_px * (1 + cfg.stop_pct),
                               exact_buy_shares=qty, signal="STOCK_T0_STOP_BUYBACK")
        elif price <= leg_px * (1 - cfg.grid_step) or dv <= cfg.close_leg_dev:
            res = self._submit(sym, "BUY", price, exact_buy_shares=qty,
                               signal="STOCK_T0_BUYBACK")
        else:
            return False
        if res is None or not res.ok or res.fill is None:
            return False
        qty_f = int(res.fill.quantity)
        st["bought_qty"] = st.get("bought_qty", 0) + qty_f
        st["day_pnl"] = st.get("day_pnl", 0.0) + (leg_px - float(res.fill.price)) * qty_f
        st.setdefault("legs", []).remove(leg)
        return True

    def _buyback_remain(self, sym: str, price: float, remain: int, st: dict) -> bool:
        """尾盘一次性买回当日卖出未回补部分（精确回补，底仓不变；被拦截则底仓净减）。"""
        res = self._submit(sym, "BUY", price, exact_buy_shares=remain,
                           signal="STOCK_T0_BUYBACK")
        if res is None or not res.ok or res.fill is None:
            return False
        qty = int(res.fill.quantity)
        st["bought_qty"] = st.get("bought_qty", 0) + qty
        return True

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _slice_qty(base_shares: int, cfg: StockT0Config) -> int:
        # 底仓不足 200 股（一手做T + 一手底仓）不做T
        if base_shares < 200:
            return 0
        raw = base_shares * cfg.t_slice_ratio
        qty = int(raw // 100 * 100)
        if qty < 100:
            qty = 100                      # 不足一手补一手（有底仓空间时）
        # 绝不清仓：至少留 100 股底仓
        return min(qty, base_shares - 100)

    @staticmethod
    def _interval_ok(last_ts: str, now_ts, minutes: int) -> bool:
        if not last_ts:
            return True
        try:
            last = dtime(*[int(x) for x in str(last_ts).split(":")[:2]])
        except Exception:                              # noqa: BLE001
            return True
        now = now_ts if isinstance(now_ts, dtime) else _parse_t(str(now_ts)[:5])
        diff = (now.hour * 60 + now.minute) - (last.hour * 60 + last.minute)
        return diff >= minutes

    def _notify_risk(self, sym: str, message: str) -> None:
        try:
            self.ctx.notifier.notify(f"个股做T {sym}", message,
                                     level="WARN", key=f"stock_t0:{self.jr.today}:{sym}")
        except Exception as exc:                       # noqa: BLE001
            logger.warning("个股做T 通知失败: %s", exc)
        try:
            self.ctx.repos.risk_events.add(
                "GATE1", "STOCK_T0_GUARD", message, symbol=sym,
                severity="WARN", trade_date=self.jr.today)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("个股做T 风控事件落库失败: %s", exc)


def stock_t0_tick(jr) -> Any:
    """调度入口：JobRunner → 个股存量持仓做T 盘中巡检（独立于主策略 intraday）。"""
    from ..scheduler.jobs import JobResult
    try:
        out = StockT0LiveRunner(jr).tick()
    except Exception as exc:                           # noqa: BLE001
        logger.exception("个股做T 巡检异常")
        return JobResult(JOB_NAME, ok=False, reason=f"{type(exc).__name__}: {exc}")
    if out.get("skipped"):
        return JobResult(JOB_NAME, skipped=True, reason=out.get("reason", "skipped"))
    return JobResult(JOB_NAME, data=out)


__all__ = ["StockT0Config", "StockT0Backtester", "StockT0LiveRunner",
           "stock_t0_tick", "mom_sell_ok", "JOB_NAME"]


