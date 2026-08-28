"""独立策略共享基类（策略实验室：打板/二板/尾盘低吸/趋势买点）。

设计（2026-08-15，与 tail_pick 正交，绝不改动既有策略）：
* 复用执行层 P7 组件：SimGateway（撮合）+ CostModel（成本）+ PortfolioState（记账），
  回测与实盘同口径。
* 每个策略继承 ``StandaloneBacktester`` 实现自己的 ``_on_day``（当日选股+买入+持仓管理），
  基类提供：行情内存预热（_bar/_hist/_last_prices）、买卖撮合（_buy/_sell）、
  权益记录、P0 成本归因 + P1 离场归因（复用 tail_pick 的 build_* 函数）。
* PIT 纪律：``_screen`` 只用 ≤ T-1 数据选池；T 日开盘买入的只允许用 T 日 open，
  T 日收盘买入的才允许用 T 日 close —— 由各策略自己遵守，基类只提供取数。

结果对象 ``StrategyResult`` 与 CLI/server 报告的约定字段一致：
metrics / trades(fills) / closed_trades / equity_curve / details /
cost_attribution / gap_attribution / open_positions。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..backtest.metrics import performance
from ..core.trading import Fill, Order, OrderType, Side
from ..datahub.types import Adjust, Freq
from ..execution.costs import CostModel
from ..execution.gateway.simulator import SimGateway
from ..portfolio.state import PortfolioState
from ..strategies.tail_pick import build_cost_attribution, build_gap_attribution

logger = logging.getLogger("strategies.base")


def load_config(settings, config_class, sid: str):
    """从 settings.yaml 的 ``strategies.<sid>`` 加载策略配置（只取已知字段）。"""
    cfg = settings.section(f"strategies.{sid}") or {}
    known = set(getattr(config_class, "__dataclass_fields__", {}))
    kw = {k: v for k, v in cfg.items() if k in known}
    return config_class(**kw)


@dataclass
class StrategyConfig:
    """各策略共享的通用参数（子类扩展自己的字段）。"""

    enabled: bool = False
    max_positions: int = 5
    position_fraction: float = 0.2
    cash_usage_ratio: float = 0.95
    # 弱市空仓（2026-08-16 P2 迭代）：沪深300 收盘 < MA(market_ma_days) 时不开新仓。
    # 判定用上一交易日收盘（T 开盘/收盘决策均可见 ≤T-1 数据，无未来函数）。
    market_filter_enabled: bool = True
    market_ma_days: int = 20
    # 第二道均线（0 = 不启用）：>0 时要求收盘同时站上 MA(market_ma_days2)
    # （2026-08-16 P3：trend_buy 用 MA60+MA20 双确认，拦掉 MA60 之上但短期走弱的窗口）
    market_ma_days2: int = 0
    # 硬排除（与 tail_pick 同语义）
    exclude_st: bool = True
    min_list_days: int = 60
    exclude_suspended: bool = True
    exclude_limit_locked: bool = True
    allowed_boards: list[str] = field(default_factory=lambda: ["MAIN", "GEM", "STAR"])
    # 预热/窗口
    warmup_days: int = 200


@dataclass
class StrategyResult:
    equity_curve: list[float] = field(default_factory=list)
    trades: list = field(default_factory=list)          # 逐笔成交（Fill）
    closed_trades: list = field(default_factory=list)   # 平仓明细
    open_positions: list = field(default_factory=list)  # 期末未平仓快照
    metrics: dict = field(default_factory=dict)
    details: list = field(default_factory=list)
    cost_attribution: dict = field(default_factory=dict)
    gap_attribution: list = field(default_factory=list)


class StandaloneBacktester:
    """独立策略回测基类。子类实现 ``_on_day`` 即可。"""

    #: 配置段 id（settings.yaml 的 strategies.<sid>）
    sid: str = "base"
    config_class = StrategyConfig

    def __init__(self, settings, hub, *, initial_cash: float = 1_000_000.0,
                 config: StrategyConfig | None = None):
        self.settings = settings
        self.hub = hub
        self.initial_cash = initial_cash
        self.config = config or self.config_class()
        self.cost = CostModel.from_settings(settings)
        self.gateway = SimGateway()
        self.portfolio = PortfolioState(cash=initial_cash)
        self.universe: list[str] = []
        self.fills: list[Fill] = []
        self.position_meta: dict[str, dict] = {}   # 策略侧持仓元数据（止损/目标/入场日）
        self.details: list = []
        self._bars: dict[str, pd.DataFrame] = {}
        self._panel: pd.DataFrame | None = None    # 子类向量化筛选面板
        self._calendar: list[date] = []
        self._seq = 0

    # ============================================================ 主循环
    def run(self, start: date, end: date) -> StrategyResult:
        days = self._trading_days(start, end)
        if len(days) < 3:
            return StrategyResult(details=["交易日不足"])
        self.universe = self._universe(days[0])
        self._prewarm(start, end)
        instr_map = self._instrument_map(self.universe)
        result = StrategyResult()
        logger.info("策略 %s 回测启动 %s ~ %s（标的 %d）", self.sid, start, end, len(self.universe))
        for i, d in enumerate(days[:-1]):
            next_day = days[i + 1]
            self.portfolio.mark_t1(next_day)
            self._on_day(d, next_day, instr_map)
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
        result.metrics = performance(result.equity_curve, trades=result.trades,
                                     realized_log=self.portfolio.realized_log)
        result.cost_attribution = build_cost_attribution(
            self.cost, result.trades, result.closed_trades, self.initial_cash)
        result.gap_attribution = build_gap_attribution(self.cost, result.closed_trades)
        return result

    # ============================================================ 子类钩子
    def _on_day(self, d: date, next_day: date, instr_map: dict) -> None:
        raise NotImplementedError

    # ============================================================ 撮合
    @staticmethod
    def _to_bar(bar: dict | None):
        """dict bar → 撮合用 Bar（SimGateway 需要属性访问）。"""
        if bar is None:
            return None
        from ..datahub.types import Bar
        return Bar(symbol=bar.get("symbol") or "", date=bar.get("date"),
                   open=float(bar.get("open") or 0), high=float(bar.get("high") or 0),
                   low=float(bar.get("low") or 0), close=float(bar.get("close") or 0),
                   volume=float(bar.get("volume") or 0), amount=float(bar.get("amount") or 0),
                   limit_up=bar.get("limit_up"), limit_down=bar.get("limit_down"))

    def _buy(self, sym: str, ref_price: float, day: date, *,
             signal: str = "BUY", notional: float | None = None,
             meta: dict | None = None) -> Fill | None:
        """T 日买入（限价单，撮合价 = ref×(1+买入滑点)）。ref 由子类给定（开盘/收盘）。"""
        cash = self.portfolio.cash
        max_n = notional if notional is not None else cash * self.config.position_fraction
        notional = min(max_n, cash * self.config.cash_usage_ratio)
        if ref_price <= 0 or notional <= 0:
            return None
        shares = int(notional / ref_price // 100 * 100)
        if shares < 100:
            return None
        self._seq += 1
        order = Order(order_id=f"{self.sid}_buy_{day.isoformat()}_{self._seq}_{sym}",
                      symbol=sym, side=Side.BUY, quantity=shares,
                      price=round(ref_price, 4), order_type=OrderType.LIMIT)
        fill = self.gateway.submit(order, day, self._to_bar(self._bar(sym, day)), self.cost)
        if fill is None:
            return None
        self.portfolio.apply_fill(fill, fill.total_fee, is_buy=True, signal=signal, asof=day)
        pos = self.portfolio.positions.get(sym)
        if pos is not None:
            pos.opened_at = day
        self.fills.append(fill)
        if meta:
            self.position_meta[sym] = dict(meta or {}, entry_ref=ref_price, opened_at=day)
        return fill

    def _sell(self, sym: str, ref_price: float, day: date, *, signal: str = "SELL",
              qty: int | None = None, market: bool = False) -> Fill | None:
        """卖出（限价/市价）。ref 由子类给定。"""
        pos = self.portfolio.positions.get(sym)
        if pos is None:
            return None
        shares = pos.can_use if qty is None else min(int(qty), pos.can_use)
        if shares <= 0:
            return None
        self._seq += 1
        order = Order(order_id=f"{self.sid}_sell_{day.isoformat()}_{self._seq}_{sym}",
                      symbol=sym, side=Side.SELL, quantity=shares,
                      price=None if market else round(ref_price, 4),
                      order_type=OrderType.MARKET if market else OrderType.LIMIT)
        fill = self.gateway.submit(order, day, self._to_bar(self._bar(sym, day)), self.cost)
        if fill is None:
            return None
        self.portfolio.apply_fill(fill, fill.total_fee, is_buy=False, signal=signal, asof=day)
        self.fills.append(fill)
        if sym not in self.portfolio.positions:
            self.position_meta.pop(sym, None)
        return fill

    def _close_all(self, day: date, prices: dict[str, float], signal: str = "FORCE_CLOSE"):
        for sym in list(self.portfolio.positions):
            px = prices.get(sym)
            if px and px > 0:
                self._sell(sym, px, day, signal=signal)

    # ============================================================ 行情
    def _prewarm(self, start: date, end: date) -> None:
        fixed_start = start - timedelta(days=self.config.warmup_days)
        try:
            df = self.hub.get_bars(self.universe, Freq.D1, fixed_start, end,
                                   Adjust.NONE, validate=True)
        except Exception as exc:  # noqa: BLE001 - 预热失败不阻塞，逐日取数兜底
            logger.warning("策略 %s 行情预热失败: %s", self.sid, exc)
            return
        if df is None or df.empty:
            return
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        self._bars = {s: sub.sort_values("date").reset_index(drop=True)
                      for s, sub in df.groupby("symbol")}
        self._fixed_start, self._bt_end = fixed_start, end
        self._prewarm_index(start, end)

    def _prewarm_index(self, start: date, end: date) -> None:
        """沪深300 指数日线 + MA（弱市空仓判定用，T-1 收盘 vs 其 MA）。"""
        self._idx_cal: list[date] = []
        self._idx_close: dict[date, float] = {}
        self._idx_ma: dict[date, float] = {}
        self._idx_ma2: dict[date, float] = {}
        try:
            idx = self.hub.get_index_bars(
                "000300.SH", start - timedelta(days=self.config.warmup_days), end)
            if idx is None or idx.empty or "close" not in idx.columns:
                return
            df = idx.copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
            self._idx_cal = sorted(df["date"].dt.date.unique())
            self._idx_close = dict(zip(df["date"].dt.date, df["close"].astype(float)))
            ma = df["close"].astype(float).rolling(
                self.config.market_ma_days, min_periods=self.config.market_ma_days).mean()
            self._idx_ma = dict(zip(df["date"].dt.date, ma))
            if self.config.market_ma_days2 > 0:
                ma2 = df["close"].astype(float).rolling(
                    self.config.market_ma_days2,
                    min_periods=self.config.market_ma_days2).mean()
                self._idx_ma2 = dict(zip(df["date"].dt.date, ma2))
        except Exception as exc:  # noqa: BLE001
            logger.warning("策略 %s 指数预热失败（弱市空仓降级放行）: %s", self.sid, exc)

    def _market_ok(self, day: date) -> bool:
        """弱市空仓：上一交易日沪深300 收盘 > MA(market_ma_days)（可选叠加第二道
        MA(market_ma_days2)）才允许开新仓。数据缺失/未知按放行处理。"""
        if not self.config.market_filter_enabled:
            return True
        prev = [x for x in self._idx_cal if x < day]
        if not prev:
            return True
        p = prev[-1]
        c, m = self._idx_close.get(p), self._idx_ma.get(p)
        if c is None or m is None or (isinstance(m, float) and not np.isfinite(m)):
            return True
        if not c > m:
            return False
        if self.config.market_ma_days2 > 0:
            m2 = self._idx_ma2.get(p)
            if m2 is not None and (not isinstance(m2, float) or np.isfinite(m2)):
                return bool(c > m2)
        return True

    def _hist(self, symbol: str) -> pd.DataFrame | None:
        """标的在预热窗口内的日线长表（已按日期升序）。"""
        df = self._bars.get(symbol)
        if df is not None and not df.empty:
            return df
        try:
            df = self.hub.get_bars(symbol, Freq.D1, self._fixed_start, self._bt_end,
                                   Adjust.NONE, validate=True)
        except Exception:  # noqa: BLE001
            return None
        if df is None or df.empty:
            return None
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        self._bars[symbol] = df
        return df

    def _bar(self, symbol: str, day: date) -> dict | None:
        """当日 bar dict（含 open/high/low/close/volume/limit_up/limit_down/prev_close）。"""
        sub = self._hist(symbol)
        if sub is None or sub.empty:
            return None
        m = sub["date"] == pd.Timestamp(day)
        if not m.any():
            return None
        row = sub.loc[m].iloc[-1]
        out = {c: row.get(c) for c in
               ("open", "high", "low", "close", "volume", "amount",
                "limit_up", "limit_down", "prev_close", "is_suspended")}
        out["symbol"] = symbol
        out["date"] = day
        return out

    def _close_px(self, symbol: str, day: date) -> float | None:
        bar = self._bar(symbol, day)
        if bar is None:
            return None
        try:
            return float(bar["close"])
        except (TypeError, ValueError):
            return None

    def _last_prices(self, day: date) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym in list(self.portfolio.positions):
            sub = self._hist(sym)
            if sub is None or sub.empty:
                continue
            sd = sub[sub["date"] <= pd.Timestamp(day)]
            if not sd.empty:
                out[sym] = float(sd.iloc[-1]["close"])
        return out

    # ============================================================ 工具
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

    # ---- 硬排除（与 tail_pick 同语义，供子类筛选用）----
    def _hard_ok(self, sym: str, day: date, instr) -> bool:
        if self.config.exclude_st and instr and getattr(instr, "is_st", False):
            return False
        if self.config.min_list_days and instr and getattr(instr, "list_date", None):
            try:
                if (day - instr.list_date).days < self.config.min_list_days:
                    return False
            except TypeError:
                pass
        bar = self._bar(sym, day)
        if bar is None:
            return False
        if self.config.exclude_suspended and bool(bar.get("is_suspended", False)):
            return False
        board = getattr(instr, "board", None) or ""
        if not board:
            try:
                from ..core.instruments import detect_board, normalize_symbol
                board = detect_board(normalize_symbol(sym)).value
            except Exception:  # noqa: BLE001
                board = ""
        if board and board not in set(self.config.allowed_boards):
            return False
        return True

    @staticmethod
    def _is_limit_up(close: float, limit_up: float, tol: float = 0.999) -> bool:
        return bool(limit_up and limit_up > 0 and close >= limit_up * tol)

    @staticmethod
    def _limit_pct(board: str) -> float:
        if board in ("GEM", "STAR"):
            return 0.20
        if board == "BSE":
            return 0.30
        return 0.10
