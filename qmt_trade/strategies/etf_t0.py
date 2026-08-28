"""ETF T+0 日内回转（底仓做T）独立策略。

设计（2026-08-17，与 tail_pick / strategylab 正交，绝不改动既有策略）
--------------------------------------------------------------------
目标：在 ETF 底仓上做日内 T+0 回转，赚取日内波动价差，底仓隔夜不动。
适用：跨境 ETF（纳指/标普/日经等 513100.SH / 513500.SH）、黄金 ETF（518880.SH）
等流动性好、可日内回转的品种；T+1 的宽基 ETF（510300.SH）可做「先卖后买」等效 T0。

纪律（与主策略共享风控链，P3 绝不绕过）：
- 底仓：无持仓时在开腿窗口内按 base_fraction（占初始资金比例）建仓，持有不动；
  只对 T 仓（日内回转仓）做买卖。建仓当日不做 T（T+1 当日买入不可卖）。
- 先卖后买为主：现价高出当日 VWAP 达 sell_dev_threshold → 卖出 T-slice；
  回落（网格 grid_step / 回归 VWAP close_leg_dev）后买回。
  先买后卖为辅（仅回测评估真 T+0 标的，same_day_roundtrip=true）：
  现价低于 VWAP 达 buy_dev_threshold → 买入 T-slice，回升后卖出。
- 单标的同时最多 1 条开腿；开腿间隔 ≥ min_interval_minutes；
  单日每标的最多 max_trades_per_symbol_per_day 次开腿。
- 开腿窗口 09:35~14:30；14:30 后只平不开；force_flat_time 强平全部 T 仓
  （当日 T 仓归零，绝不过夜）。
- 单腿反向移动 stop_pct 即止损平腿；单日 T0 净亏损超
  max_daily_loss_pct × 总资产 → 当日停开新腿。
- 回测与实盘同口径：SimGateway（撮合）+ CostModel（成本）+ PortfolioState（记账），
  P7 精神；实盘经 ctx.execution.submit_intent 走完整风控链
  （OrderGuard 防重/限频 → Gate-1 → KillSwitch → Gateway → 记账）。

实盘 v1 已知限制（诚实声明）
- 买入腿股数由共享 PositionSizer 决定（等权配置下目标 ~12.5% 仓位），无法精确
  等于卖出量；且 A 股 T+1 账本下当日买入份额当日不可卖，因此【实盘只做先卖后买】，
  same_day_roundtrip 仅用于回测评估真 T+0 标的。
  买回前先预览 sizer 结果：估算金额超 buyback_max_notional_ratio × 总资产则放弃
  买回（宁可底仓净减，绝不无控制加仓），并在日志/风控事件中记录；当日收盘对账
  sold_qty vs bought_qty，净减部分即为底仓降低（等效减仓）。
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

#: ETF T+0 调度任务名（与 scheduler/runner.py 的 JobSpec.name 一致）
JOB_NAME = "etf_t0_intraday"


def _parse_t(text: str) -> dtime:
    """'09:35' → datetime.time(9, 35)。解析失败返回 00:00（保守：视为最早可交易）。"""
    try:
        h, m = str(text).split(":")
        return dtime(int(h), int(m))
    except Exception:                                # noqa: BLE001
        return dtime(0, 0)


def mom_sell_ok(cfg: ETFT0Config, mom: float, sym: str | None = None) -> bool:
    """卖高腿的动量门槛（off/filter/confirm 三模式）。回测与实盘共用（P7）。

    按标解析 momentum_override（sym 提供时），否则用全局参数。
    - off:     恒 True（纯卖高买低网格）
    - filter:  逆势过滤，未强涨才卖高（mom < threshold）
    - confirm: 顺势确认，强涨才卖高（mom >= threshold）
    """
    mode, _win, thr = cfg.momentum_params_for(sym or "")
    if mode == "confirm":
        return mom >= thr
    if mode == "filter":
        return mom < thr
    return True


def mom_buy_ok(cfg: ETFT0Config, mom: float, sym: str | None = None) -> bool:
    """买低腿的动量门槛（off/filter/confirm；仅 same_day_roundtrip 回测用）。"""
    mode, _win, thr = cfg.momentum_params_for(sym or "")
    if mode == "confirm":
        return mom <= -thr
    if mode == "filter":
        return mom > -thr
    return True


@dataclass
class ETFT0Config(StrategyConfig):
    """ETF T+0 全部参数，从 ``config/settings.yaml::strategies.etf_t0`` 加载。"""

    #: 标的池（支持日内回转的 ETF；T+1 标的请关闭 same_day_roundtrip）
    symbols: list[str] = field(default_factory=lambda: ["513100.SH", "518880.SH"])
    #: 每只底仓占初始资金比例（回测口径；实盘作 max_weight_hint 提示）
    base_fraction: float = 0.12
    #: 每次做T股数 = 底仓股数 × 该比例（不足一手则取一手）
    t_slice_ratio: float = 0.30
    #: True=回测支持先买后卖（仅真 T+0 标的）；实盘恒为先卖后买
    same_day_roundtrip: bool = False
    #: 开腿信号：现价高于当日 VWAP 该比例 → 卖出做T
    sell_dev_threshold: float = 0.008
    #: 开腿信号：现价低于当日 VWAP 该比例 → 买入做T（需 same_day_roundtrip）
    buy_dev_threshold: float = 0.008
    #: 平腿信号：偏离回归到该比例内（回 VWAP）
    close_leg_dev: float = 0.002
    #: 平腿信号：相对开腿价移动该比例（网格止盈）
    grid_step: float = 0.005
    #: 单腿止损：反向移动该比例即止损平腿（接受该笔亏损）
    stop_pct: float = 0.005
    #: 单日每标的最多开腿次数（OrderGuard 单标日上限 6 笔 / 2 腿≈4~6 笔）
    max_trades_per_symbol_per_day: int = 2
    #: 开腿最小间隔（分钟）
    min_interval_minutes: int = 5
    #: 开腿窗口
    open_t_start: str = "09:35"
    open_t_end: str = "14:30"
    #: 尾盘强平时刻（当日 T 仓归零）
    force_flat_time: str = "14:50"
    #: 单日 T0 净亏损上限（占总资产比例），触发当日停开新腿
    max_daily_loss_pct: float = 0.003
    #: 实盘买回预览上限：sizer 估算买回金额 > 该比例 × 总资产 → 放弃买回（防加仓）
    buyback_max_notional_ratio: float = 0.03
    #: 当日分钟线不足该根数不参与做T（数据质量护栏）
    min_minutes_per_day: int = 5
    #: 日内动量模式（VWAP 回归增强，2026-08-18）：开腿前检查过去
    #: momentum_window_min 分钟的涨跌幅（动量）。
    #:   off     = 不启用（等价旧版纯卖高买低网格）
    #:   filter  = 逆势过滤：动量超 threshold 视为强趋势，禁止逆势开腿
    #:             （卖高腿要求未强涨；买低腿要求未强跌）——防"卖了继续涨/买了继续跌"
    #:   confirm = 顺势确认：动量必须同向超 threshold 才开腿
    #:             （卖高腿要求强涨；买低腿要求强跌）——趋势延续假设
    momentum_mode: str = "filter"
    #: 动量回看窗口（分钟）
    momentum_window_min: int = 15
    #: 动量阈值：窗口涨/跌幅超此比例视为强趋势（filter 禁止 / confirm 要求）
    momentum_threshold: float = 0.004
    #: 按标的底仓占比覆盖（per-symbol override）。用于把复测筛出的 20%/80% 组合直接落地。
    base_fraction_override: dict[str, float] = field(default_factory=dict)
    #: 开盘后前 N 分钟振幅过低则过滤当日开腿；0=关闭。
    low_vol_window_min: int = 0
    #: 低波动过滤阈值：窗口内 high/low - 1 低于该比例则不开腿。
    low_vol_min_amplitude_pct: float = 0.0
    #: 开盘冲高后回落过滤：截至当前最高涨幅达到该值且从高点回落达到 fade 阈值则不开腿；0=关闭。
    rush_up_pct: float = 0.0
    #: 开盘冲高后回落过滤：从日内高点回落比例达到该值则不开腿。
    rush_fade_pct: float = 0.0
    #: 按标的动量参数覆盖（per-symbol override）：{symbol: {momentum_mode, momentum_window_min,
    #: momentum_threshold}}。未列出的标的用全局 momentum_mode/window/threshold。
    #: 例：{"513310.SH": {"momentum_mode": "confirm", "momentum_threshold": 0.015}}
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
class ETFT0Backtester:
    """ETF T+0 分钟级回测器（自包含，复用 SimGateway/CostModel/PortfolioState）。

    与主因子体系 / tail_pick 完全独立：只对配置的 ETF 标的做底仓 + 日内回转，
    不经过共享 RiskEngine / PositionSizer（与 tail_pick 回测同模式）。
    """

    sid = "etf_t0"
    config_class = ETFT0Config

    def __init__(self, settings, hub, *, initial_cash: float = 1_000_000.0,
                 config: ETFT0Config | None = None):
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
        self._seq = 0
        self._minute_available = False

    # ------------------------------------------------------------ 主入口
    def run(self, start: date, end: date) -> StrategyResult:
        cfg = self.config
        universe = [s for s in (cfg.symbols or []) if s]
        if not universe:
            return StrategyResult(details=["未配置 ETF 标的（strategies.etf_t0.symbols）"])
        days = self._trading_days(start, end)
        if len(days) < 3:
            return StrategyResult(details=["交易日不足"])
        probe = self._minute_bars(universe[0], days[0])
        self._minute_available = probe is not None and len(probe) > 1
        logger.info("ETF T+0 回测 %s~%s 标的=%s 分钟线=%s", start, end, universe,
                    "可用" if self._minute_available else "不可用（仅持底仓，无做T）")

        result = StrategyResult()
        t0 = {"legs": 0, "closed": 0, "wins": 0, "losses": 0, "pnl": 0.0,
              "days_traded": 0, "day_pnl": []}
        base_done = False
        for i, d in enumerate(days):
            if i + 1 < len(days):
                self.portfolio.mark_t1(days[i + 1])
            if not base_done:
                self._enter_base(d, universe)
                base_done = True
            day_pnl = self._intraday_t0(d, universe, t0)
            if abs(day_pnl) > 1e-9:
                t0["days_traded"] += 1
            t0["pnl"] += day_pnl  # 修复：此前 t0['pnl'] 初始化后从未累加，CLI 的 t0_pnl 恒为 0
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
            result.metrics["minute_available"] = self._minute_available
        self.details = [
            f"ETF T+0 回测：标的={universe}，分钟线={'可用' if self._minute_available else '不可用'}",
            f"做T腿数={t0['legs']}，平腿={t0['closed']}，胜腿={t0['wins']}，亏腿={t0['losses']}，"
            f"T0净盈亏={t0['pnl']:.2f}，有T交易日={t0['days_traded']}",
        ]
        result.details = list(self.details)
        result.minute_available = self._minute_available
        return result

    @staticmethod
    def _mom_sell_ok(cfg: ETFT0Config, mom: float, sym: str = "") -> bool:
        """卖高腿的动量门槛（off/filter/confirm 三模式）。回测与实盘共用（P7）。"""
        return mom_sell_ok(cfg, mom, sym)

    @staticmethod
    def _mom_buy_ok(cfg: ETFT0Config, mom: float, sym: str = "") -> bool:
        """买低腿的动量门槛（off/filter/confirm；仅 same_day_roundtrip 回测用）。"""
        return mom_buy_ok(cfg, mom, sym)

    # ------------------------------------------------------------ 建仓
    def _enter_base(self, day: date, universe: list[str]) -> None:
        """首个有行情交易日开盘按 base_fraction 建底仓（每标的一笔，此后不动）。"""
        cfg = self.config
        for sym in universe:
            bar = self._bar(sym, day)
            if bar is None:
                continue
            px = float(bar.get("open") or 0)
            if px <= 0:
                continue
            notional = min(self.initial_cash * cfg.base_fraction,
                           self.portfolio.cash * 0.98)
            if notional <= 0:
                continue
            shares = int(notional / px // 100 * 100)
            if shares < 100:
                continue
            self._fill(sym, "BUY", shares, px, self._to_bar(bar), day, "ETF_T0_BASE")

    # ------------------------------------------------------------ 日内回转
    def _intraday_t0(self, day: date, universe: list[str], t0: dict) -> float:
        """对每个标的跑当日分钟级 T0 回转，返回当日 T0 净盈亏。"""
        cfg = self.config
        open_start, open_end = _parse_t(cfg.open_t_start), _parse_t(cfg.open_t_end)
        force_flat = _parse_t(cfg.force_flat_time)
        day_pnl = 0.0
        day_blocked = False
        for sym in universe:
            minute = self._minute_bars(sym, day)
            if minute is None or len(minute) < cfg.min_minutes_per_day:
                continue
            pos = self.portfolio.positions.get(sym)
            if pos is None or pos.shares <= 0:
                continue
            base_shares = pos.shares
            slice_qty = self._slice_qty(base_shares, cfg)
            if slice_qty <= 0 or slice_qty > base_shares:
                continue
            # 按标解析动量参数（momentum_override 优先，P7 与实盘同口径）
            _m_mode, mom_window, _m_thr = cfg.momentum_params_for(sym)
            bars = minute.reset_index(drop=True)
            closes = bars["close"].astype(float).to_numpy()
            vols = bars["volume"].astype(float).to_numpy()
            times = bars["time"].tolist()
            cum_vol = pd.Series(vols).cumsum()
            cum_pv = pd.Series(closes * vols).cumsum()
            vwap = (cum_pv / cum_vol.replace(0, float("nan"))).to_numpy()

            legs: list[dict] = []
            sold_qty = bought_qty = 0
            opened_today = 0
            last_leg_idx: int | None = None

            for idx in range(len(bars)):
                t = times[idx]
                px = float(closes[idx])
                vw = float(vwap[idx])
                dv = (px / vw - 1.0) if (not math.isnan(vw) and vw > 0) else 0.0
                # 窗口动量：过去 mom_window 分钟涨跌幅（不足窗口用首根近似）
                mom = 0.0
                if mom_window > 0:
                    ref_i = max(0, idx - mom_window)
                    ref_px = float(closes[ref_i])
                    mom = (px / ref_px - 1.0) if ref_px > 0 else 0.0
                bar = self._bar_from_row(bars.iloc[idx], sym, day)
                # ---- 1) 平腿优先（止损 → 网格/回归）----
                for leg in list(legs):
                    closed = False
                    leg_px = float(leg["price"])
                    if leg["side"] == "SELL":                     # 等买回
                        if px >= leg_px * (1 + cfg.stop_pct):     # 反向止损
                            ref = leg_px * (1 + cfg.stop_pct)
                            if self._fill(sym, "BUY", int(leg["qty"]), ref, bar, day,
                                          "ETF_T0_STOP_BUYBACK"):
                                day_pnl += (leg_px - ref) * int(leg["qty"])
                                self._settle_leg(t0, leg, False)
                                closed = True
                        elif px <= leg_px * (1 - cfg.grid_step) or dv <= cfg.close_leg_dev:
                            if self._fill(sym, "BUY", int(leg["qty"]), px, bar, day,
                                          "ETF_T0_BUYBACK"):
                                day_pnl += (leg_px - px) * int(leg["qty"])
                                self._settle_leg(t0, leg, True)
                                closed = True
                    else:                                          # BUY 腿，等卖出
                        if px <= leg_px * (1 - cfg.stop_pct):     # 反向止损
                            ref = leg_px * (1 - cfg.stop_pct)
                            if self._fill(sym, "SELL", int(leg["qty"]), ref, bar, day,
                                          "ETF_T0_STOP_EXIT"):
                                day_pnl += (ref - leg_px) * int(leg["qty"])
                                self._settle_leg(t0, leg, False)
                                closed = True
                        elif px >= leg_px * (1 + cfg.grid_step) or dv >= -cfg.close_leg_dev:
                            if self._fill(sym, "SELL", int(leg["qty"]), px, bar, day,
                                          "ETF_T0_EXIT"):
                                day_pnl += (px - leg_px) * int(leg["qty"])
                                self._settle_leg(t0, leg, True)
                                closed = True
                    if closed:
                        legs.remove(leg)

                # ---- 2) 开新腿（窗口内、无开腿、未超限、未触发日亏熔断）----
                if (not day_blocked and not legs
                        and opened_today < cfg.max_trades_per_symbol_per_day
                        and open_start <= t <= open_end):
                    if last_leg_idx is not None and \
                            (idx - last_leg_idx) < cfg.min_interval_minutes:
                        pass                                    # 间隔不足，跳过本分钟
                    elif (dv >= cfg.sell_dev_threshold
                          and self._mom_sell_ok(cfg, mom, sym)   # 动量模式：off/filter/confirm
                          and sold_qty + slice_qty <= base_shares):
                        if self._fill(sym, "SELL", slice_qty, px, bar, day, "ETF_T0_SELL"):
                            legs.append({"side": "SELL", "qty": slice_qty, "price": px})
                            sold_qty += slice_qty
                            opened_today += 1
                            last_leg_idx = idx
                            t0["legs"] += 1
                    elif (cfg.same_day_roundtrip and dv <= -cfg.buy_dev_threshold
                          and self._mom_buy_ok(cfg, mom, sym)   # 动量模式：off/filter/confirm
                          and bought_qty + slice_qty <= base_shares
                          and self.portfolio.cash >= slice_qty * px * 1.01):
                        if self._fill(sym, "BUY", slice_qty, px, bar, day, "ETF_T0_BUY"):
                            legs.append({"side": "BUY", "qty": slice_qty, "price": px})
                            bought_qty += slice_qty
                            opened_today += 1
                            last_leg_idx = idx
                            t0["legs"] += 1

                # ---- 3) 尾盘强平（当日 T 仓归零）----
                if t >= force_flat and legs:
                    for leg in list(legs):
                        leg_px = float(leg["price"])
                        if leg["side"] == "SELL":
                            self._fill(sym, "BUY", int(leg["qty"]), px, bar, day,
                                       "ETF_T0_FORCE_FLAT")
                            day_pnl += (leg_px - px) * int(leg["qty"])
                        else:
                            self._fill(sym, "SELL", int(leg["qty"]), px, bar, day,
                                       "ETF_T0_FORCE_FLAT")
                            day_pnl += (px - leg_px) * int(leg["qty"])
                        self._settle_leg(t0, leg,
                                         px < leg_px if leg["side"] == "SELL" else px > leg_px)
                    legs.clear()

                if day_pnl <= -cfg.max_daily_loss_pct * self.initial_cash:
                    day_blocked = True

            # ---- 兜底：最后一根仍未平的腿按收盘价强平 ----
            if legs and len(bars):
                px = float(closes[-1])
                bar = self._bar_from_row(bars.iloc[-1], sym, day)
                for leg in list(legs):
                    leg_px = float(leg["price"])
                    if leg["side"] == "SELL":
                        self._fill(sym, "BUY", int(leg["qty"]), px, bar, day, "ETF_T0_FORCE_FLAT")
                        day_pnl += (leg_px - px) * int(leg["qty"])
                        self._settle_leg(t0, leg, px < leg_px)
                    else:
                        self._fill(sym, "SELL", int(leg["qty"]), px, bar, day, "ETF_T0_FORCE_FLAT")
                        day_pnl += (px - leg_px) * int(leg["qty"])
                        self._settle_leg(t0, leg, px > leg_px)
                legs.clear()
        return day_pnl

    def _settle_leg(self, t0: dict, leg: dict, win: bool) -> None:
        t0["closed"] += 1
        if win:
            t0["wins"] += 1
        else:
            t0["losses"] += 1

    @staticmethod
    def _slice_qty(base_shares: int, cfg: ETFT0Config) -> int:
        qty = int(base_shares * cfg.t_slice_ratio // 100 * 100)
        return max(100, qty) if qty > 0 else 0

    # ------------------------------------------------------------ 撮合与行情
    def _fill(self, sym: str, side: str, qty: int, ref_price: float,
              bar: Bar | None, day: date, signal: str) -> Fill | None:
        side_enum = Side.BUY if side == "BUY" else Side.SELL
        if side_enum is Side.SELL:
            pos = self.portfolio.positions.get(sym)
            if pos is None or pos.shares < qty:
                return None
            # T+1 账本纪律：非真 T+0 标的只允许卖「可卖数量」（隔夜底仓）；
            # 真 T+0（same_day_roundtrip=true，跨境/黄金 ETF）允许当日回转。
            if not self.config.same_day_roundtrip and pos.can_use < qty:
                return None
        if bar is None or ref_price <= 0:
            return None
        self._seq += 1
        order = Order(order_id=f"etf0_{day.isoformat()}_{self._seq}_{sym}",
                      symbol=sym, side=side_enum, quantity=int(qty),
                      price=round(ref_price, 4), order_type=OrderType.LIMIT)
        fill = self.gateway.submit(order, day, bar, self.cost)
        if fill is None:
            return None
        self.portfolio.apply_fill(fill, fill.total_fee, is_buy=(side_enum is Side.BUY),
                                  signal=signal, asof=day)
        if side_enum is Side.BUY:
            # 记录建仓日，mark_t1 才能按 T+1 解冻可卖数量
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
        """当日日线 bar dict（建底仓用）。"""
        try:
            df = self.hub.get_bars([sym], Freq.D1, day, day, Adjust.NONE, validate=True)
        except Exception:                              # noqa: BLE001
            return None
        if df is None or df.empty:
            return None
        row = df.iloc[-1]
        out = {c: row.get(c) for c in ("open", "high", "low", "close", "volume",
                                       "amount", "prev_close", "limit_up",
                                       "limit_down", "is_suspended")}
        out["symbol"] = sym
        out["date"] = day
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
class ETFT0LiveRunner:
    """ETF T+0 实盘盘中管理器（由调度任务 ``etf_t0_intraday`` 周期性调用）。

    与 LabLiveRunner 不同：ETF T+0 是**盘中高频**策略，不走 strategylab 的
    open/close 两相位，而是挂在独立 interval 任务上（09:30~15:00 每 N 秒）。
    启停由 ``strategies.etf_t0.enabled`` 控制（WebUI「策略实验室」开关即启停）。

    风控：所有订单经 ``ctx.execution.submit_intent``（OrderGuard → Gate-1 →
    KillSwitch → Gateway → 记账），与主策略同一执行链（P3/P7）。
    实盘只做【先卖后买】；买入腿数量由共享 sizer 决定，提交前先预览，超
    ``buyback_max_notional_ratio`` 则放弃（防无控制加仓）。
    """

    def __init__(self, jr):
        self.jr = jr
        self.ctx = jr.ctx
        self._regime = None
        self._seq = 0
        self._warned_roundtrip = False

    # ------------------------------------------------------------ 配置/状态
    def _cfg(self) -> ETFT0Config:
        return load_config(self.ctx.settings, ETFT0Config, "etf_t0")

    def _state_key(self) -> str:
        return f"etf_t0:state:{self.jr.today.isoformat()}"

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
                reason=f"ETF T+0 盘中状态 {self.jr.today}")
        except Exception as exc:                       # noqa: BLE001
            logger.warning("ETF T+0 状态落库失败: %s", exc)

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
    def _intent(self, sym: str, action: str, price: float,
                max_weight_hint: float) -> Any:
        from ..brain.schemas import TradeIntent
        return TradeIntent(
            symbol=sym, action=action, confidence=0.9,
            conviction="HIGH" if action in ("SELL", "REDUCE") else "MEDIUM",
            entry_type="LIMIT", entry_ref_price=round(price, 4),
            stop_loss_type="FIXED_PCT", stop_loss_value=0.005,
            risk_budget_hint=0.3, max_weight_hint=max_weight_hint,
            time_horizon_days=5, max_holding_days=20, valid_until=self.jr.today,
            reasoning=f"ETF T+0 底仓做T [{sym}]")

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
                reduce_shares: int = 0, signal: str = "ETF_T0",
                max_weight_hint: float = 0.03) -> Any:
        bar = self._make_bar(sym, price)
        if bar is None:
            return None
        it = self._intent(sym, action, price, max_weight_hint)
        self._seq += 1
        try:
            return self.ctx.execution.submit_intent(
                it, bar=bar, market_day=self.jr.today, asof=self.jr.today,
                regime=self._regime_snapshot(), instrument=self._instrument(sym),
                sym_industry={sym: ""},
                plan_id=f"etf0_{self.jr.today:%Y%m%d}",
                # LiveRunner 每轮巡检都会重新实例化，实例内递增序号会从 1 重置。
                # 使用提交时刻作为跨轮唯一序号，重复提交仍由 pending/cooldown 防线拦截。
                seq=int(datetime.now().timestamp()) + self._seq,
                signal=signal, reduce_shares=reduce_shares)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("ETF T+0 %s %s %s 下单异常: %s",
                           self.jr.today, action, sym, exc)
            return None

    def _preview_buy_shares(self, sym: str, price: float) -> int:
        """预览共享 sizer 会给多少股（用于买回金额上限护栏）。"""
        try:
            from ..portfolio.sizer import SizingContext
            snap = self._regime_snapshot()
            ta = max(self.ctx.portfolio.total_asset, 1.0)
            stop = price * (1 - 0.005)
            ctx = SizingContext(
                total_asset=ta, available_cash=self.ctx.portfolio.cash,
                entry_price=price, stop_price=stop, avg_volume_20d=1e6,
                regime=snap.regime,
                current_weight=self.ctx.portfolio.position_weight(sym),
                industry="")
            it = self._intent(sym, "BUY", price, 0.03)
            res = self.ctx.sizer.suggest(it, ctx)
            return int(res.shares) if res.ok() else 0
        except Exception:                              # noqa: BLE001
            return 0

    # ------------------------------------------------------------ 盘中巡检
    def tick(self) -> dict:
        cfg = self._cfg()
        if not cfg.enabled:
            return {"skipped": True, "reason": "enabled=false（UI 停用）"}
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

        for sym in cfg.symbols:
            bars = self._minute_bars(sym)
            if bars is None or len(bars) < cfg.min_minutes_per_day:
                continue
            closes = bars["close"].astype(float).to_numpy()
            vols = bars["volume"].astype(float).to_numpy()
            cum_vol = pd.Series(vols).cumsum()
            cum_pv = pd.Series(closes * vols).cumsum()
            vwap = float((cum_pv / cum_vol.replace(0, float("nan"))).iloc[-1])
            price = float(closes[-1])
            tv = bars.iloc[-1]["time"]
            dv = (price / vwap - 1.0) if (not math.isnan(vwap) and vwap > 0) else 0.0
            # 窗口动量（与回测同口径，按标解析 override）：过去 N 分钟涨跌幅
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
                                        "last_trade_time": "", "base_done": False})
            # ---- 底仓：无持仓时在开腿窗口内按 base_fraction 建仓，建仓当日不做T ----
            if self._ensure_base(sym, price, tv, st, cfg):
                summary["fills"] += 1
                summary["base"] += 1
                continue
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
                if st.get("sold_qty", 0) > st.get("bought_qty", 0):
                    if self._buyback_remain(sym, price, st, cfg):
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
                opened = self._open_leg(sym, price, dv, mom, st, cfg)
                if opened == "opened":
                    summary["legs_opened"] += 1
                    summary["fills"] += 1
                elif opened == "rejected":
                    summary["rejected"] += 1
        self._save_state(state)
        return summary

    # ------------------------------------------------------------ 底仓
    def _ensure_base(self, sym: str, price: float, tv, st: dict,
                     cfg: ETFT0Config) -> bool:
        """无持仓时按 base_fraction 建底仓（每个标的一次）。返回是否已成交。"""
        pos = self.ctx.portfolio.positions.get(sym)
        if pos is not None and pos.shares > 0:
            st["base_done"] = True
            return False
        if st.get("base_done"):
            return False
        open_start = _parse_t(cfg.open_t_start)
        open_end = _parse_t(cfg.open_t_end)
        if not (open_start <= tv <= open_end):
            return False
        res = self._submit(sym, "BUY", price, signal="ETF_T0_BASE",
                           max_weight_hint=max(0.02, min(0.15, cfg.base_fraction)))
        if res is not None and res.ok and res.fill is not None:
            st["base_done"] = True
            return True
        return False

    # ------------------------------------------------------------ 腿操作
    def _open_leg(self, sym: str, price: float, dv: float, mom: float, st: dict,
                  cfg: ETFT0Config) -> str:
        pos = self.ctx.portfolio.positions.get(sym)
        base_shares = int(pos.shares) if pos else 0
        slice_qty = self._slice_qty(base_shares, cfg) if base_shares > 0 else 0
        if slice_qty <= 0 or slice_qty > base_shares:
            return "skipped"
        # 实盘只做先卖后买（A 股 T+1 账本：当日买入份额当日不可卖，先买后卖无法平腿）。
        # same_day_roundtrip 仅用于回测评估真 T+0 标的。
        if cfg.same_day_roundtrip and not self._warned_roundtrip:
            self._warned_roundtrip = True
            logger.warning(
                "ETF T+0 %s：实盘不支持先买后卖（T+1 账本当日买入不可卖），"
                "same_day_roundtrip 仅在回测生效，实盘忽略该分支", self.jr.today)
        if dv >= cfg.sell_dev_threshold:
            # 动量模式判定（off/filter/confirm，按标解析 override，与回测共用）
            if not mom_sell_ok(cfg, mom, sym):
                return "skipped"
            if pos is None or pos.can_use < slice_qty:
                return "rejected"
            # 上一卖出腿尚未回补时不叠开（防累计净空）
            if st.get("sold_qty", 0) - st.get("bought_qty", 0) >= slice_qty:
                return "skipped"
            res = self._submit(sym, "REDUCE", price, reduce_shares=slice_qty,
                               signal="ETF_T0_SELL", max_weight_hint=0.03)
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
                   cfg: ETFT0Config, st: dict, *, force: bool = False) -> bool:
        leg_px = float(leg["price"])
        qty = int(leg.get("qty") or 0)
        if qty <= 0:
            st.setdefault("legs", []).remove(leg)
            return False
        if leg["side"] == "SELL":                      # 等买回
            if force:
                res = self._submit(sym, "BUY", price, signal="ETF_T0_FORCE_FLAT",
                                   max_weight_hint=self._buy_hint(sym, price, st, cfg))
            elif price >= leg_px * (1 + cfg.stop_pct):
                res = self._submit(sym, "BUY", leg_px * (1 + cfg.stop_pct),
                                   signal="STOP_LOSS",
                                   max_weight_hint=self._buy_hint(sym, price, st, cfg))
            elif price <= leg_px * (1 - cfg.grid_step) or dv <= cfg.close_leg_dev:
                res = self._submit(sym, "BUY", price, signal="ETF_T0_BUYBACK",
                                   max_weight_hint=self._buy_hint(sym, price, st, cfg))
            else:
                return False
            if res is None or not res.ok or res.fill is None:
                return False
            qty_f = int(res.fill.quantity)
            st["bought_qty"] = st.get("bought_qty", 0) + qty_f
            st["day_pnl"] = st.get("day_pnl", 0.0) + (leg_px - float(res.fill.price)) * qty_f
            st.setdefault("legs", []).remove(leg)
            return True
        # BUY 腿，等卖出（实盘不会开出；历史状态恢复时兜底）
        if force:
            res = self._submit(sym, "REDUCE", price, reduce_shares=qty,
                               signal="ETF_T0_FORCE_FLAT", max_weight_hint=0.03)
        elif price <= leg_px * (1 - cfg.stop_pct):
            res = self._submit(sym, "REDUCE", leg_px * (1 - cfg.stop_pct),
                               reduce_shares=qty, signal="STOP_LOSS",
                               max_weight_hint=0.03)
        elif price >= leg_px * (1 + cfg.grid_step) or dv >= -cfg.close_leg_dev:
            res = self._submit(sym, "REDUCE", price, reduce_shares=qty,
                               signal="ETF_T0_EXIT", max_weight_hint=0.03)
        else:
            return False
        if res is None or not res.ok or res.fill is None:
            return False
        qty_f = int(res.fill.quantity)
        st["sold_qty"] = st.get("sold_qty", 0) + qty_f
        st["day_pnl"] = st.get("day_pnl", 0.0) + (float(res.fill.price) - leg_px) * qty_f
        st.setdefault("legs", []).remove(leg)
        return True

    def _buy_hint(self, sym: str, price: float, st: dict, cfg: ETFT0Config) -> float:
        """买回 intent 的 max_weight_hint：按剩余未回补金额占组合比例给提示。"""
        remain = max(0, st.get("sold_qty", 0) - st.get("bought_qty", 0))
        ta = max(self.ctx.portfolio.total_asset, 1.0)
        hint = remain * price / ta
        return max(0.01, min(0.12, hint))

    def _buyback_remain(self, sym: str, price: float, st: dict, cfg: ETFT0Config) -> bool:
        """尾盘一次性买回当日卖出未回补部分。先预览 sizer，超护栏则放弃（底仓净减）。"""
        preview = self._preview_buy_shares(sym, price)
        ta = max(self.ctx.portfolio.total_asset, 1.0)
        if preview * price > cfg.buyback_max_notional_ratio * ta:
            logger.warning(
                "ETF T+0 %s %s 尾盘买回被护栏拦下：sizer 预览 %.0f 股/%.2f 元"
                " 超 buyback_max_notional_ratio=%.1f%%（底仓净减，不强行加仓）",
                self.jr.today, sym, preview, preview * price,
                cfg.buyback_max_notional_ratio * 100)
            self._notify_risk(sym, f"ETF T+0 买回护栏拦截（sizer 预览 {preview} 股），"
                                   "底仓净减，当日不再买回")
            return False
        res = self._submit(sym, "BUY", price, signal="ETF_T0_BUYBACK",
                           max_weight_hint=self._buy_hint(sym, price, st, cfg))
        if res is None or not res.ok or res.fill is None:
            return False
        qty = int(res.fill.quantity)
        st["bought_qty"] = st.get("bought_qty", 0) + qty
        return True

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _slice_qty(base_shares: int, cfg: ETFT0Config) -> int:
        qty = int(base_shares * cfg.t_slice_ratio // 100 * 100)
        return max(100, qty) if qty > 0 else 0

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
            self.ctx.notifier.notify(f"ETF T+0 {sym}", message,
                                     level="WARN", key=f"etf_t0:{self.jr.today}:{sym}")
        except Exception as exc:                       # noqa: BLE001
            logger.warning("ETF T+0 通知失败: %s", exc)
        try:
            self.ctx.repos.risk_events.add(
                "GATE1", "ETF_T0_GUARD", message, symbol=sym,
                severity="WARN", trade_date=self.jr.today)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("ETF T+0 风控事件落库失败: %s", exc)


def etf_t0_tick(jr) -> Any:
    """调度入口：JobRunner → ETF T+0 盘中巡检（独立于主策略 intraday）。"""
    from ..scheduler.jobs import JobResult
    try:
        out = ETFT0LiveRunner(jr).tick()
    except Exception as exc:                           # noqa: BLE001
        logger.exception("ETF T+0 巡检异常")
        return JobResult(JOB_NAME, ok=False, reason=f"{type(exc).__name__}: {exc}")
    if out.get("skipped"):
        return JobResult(JOB_NAME, skipped=True, reason=out.get("reason", "skipped"))
    return JobResult(JOB_NAME, data=out)


__all__ = ["ETFT0Config", "ETFT0Backtester", "ETFT0LiveRunner", "etf_t0_tick",
           "JOB_NAME"]
