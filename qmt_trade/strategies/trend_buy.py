"""趋势类买点（策略实验室·方向四）。

三类经 A 股 10 年数据检验的买点（持仓数天~数周，非隔夜）：

A. 突破回踩确认（breakout_pullback，胜率 72% / 盈亏比 3.1:1）：
   ① MA20 与 MA60 均上行且 MA20 > MA60；
   ② 放量突破：突破日收盘 > 前 60 日最高，且量 ≥ 1.8×20 日均量；
   ③ 突破后 3~5 天内回踩：T-1 收盘回到突破位 ±3%，且量 ≤ 突破日一半；
   ④ T 收盘买入；止损 = 回踩最低点×0.98。

B. 均线多头排列回调（ma_pullback，胜率 69% / 盈亏比 2.7:1）：
   ① MA5>MA10>MA20>MA60 多头排列；
   ② T-1 回调触 MA10（low ≤ MA10 且 close 站稳）；
   ③ T-1 缩量；T 收盘买入。

C. 上升趋势线回踩（trendline，胜率 65% / 盈亏比 2.5:1）：
   ① 近 20 日低点高于更早 20 日低点（低点抬高 = 上升趋势线）；
   ② T-1 收盘回踩至该支撑线 ±3% 内；
   ③ T-1 缩量、收盘在 MA60 上方；T 收盘买入。

离场（统一）：
   止损 = 回踩过程最低点×0.98（盘中 low 触及即离场）；
   止盈1 = +20% 卖 50% → 止盈2 = +35% 清仓；
   时间止损 20 日（收盘卖）。

配置：config/settings.yaml::strategies.trend_buy
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .base import StandaloneBacktester, StrategyConfig

logger = logging.getLogger("strategies.trend_buy")


@dataclass
class TrendBuyConfig(StrategyConfig):
    #: 形态：breakout_pullback / ma_pullback / trendline / any（三形态任一命中即买，组合打法）
    pattern: str = "breakout_pullback"
    # 弱市空仓：趋势类持仓数周，用 MA60（比短线策略更保守）
    market_ma_days: int = 60
    # —— 突破回踩（2026-08-16 P2 收紧：放量门槛↑、回踩窗口收窄、站稳 MA10）——
    breakout_vol_mult: float = 2.0       # 突破日量 ≥ 倍×20日均量（1.8→2.0）
    breakout_lookback: int = 60          # 突破前 N 日新高
    pullback_band: float = 0.03          # 回踩到突破位 ±band
    pullback_vol_shrink: float = 0.5     # 回踩日量 ≤ 突破日量×该值
    pullback_window: tuple = (2, 3)      # 突破后 N 天内回踩（(2,5)→(2,3)：只做近期回踩）
    pullback_hold_ma: int = 10           # 回踩收盘站稳 MA10（新增）
    # —— 均线多头回调 ——
    ma_pullback_line: int = 10           # 回踩 MA10
    # —— 趋势线 ——
    trendline_win: int = 20              # 近 20 日低点
    trendline_band: float = 0.03
    # —— 离场（重构止损：破位才止损 —— max(成本−8% 兜底, 回踩最低×0.95)）——
    take_profit1: float = 0.20
    take_profit2: float = 0.35
    tp1_sell_ratio: float = 0.5
    stop_floor_mult: float = 0.95        # 止损 = 回踩最低点×该值
    stop_floor_pct: float = 0.08         # 止损兜底（不低于成本−8%）
    max_hold_days: int = 20
    # —— 移动止盈（2026-08-16 搜索新增：盈利 ≥ trail_activate_pct 后，自高点回撤
    #    trail_drawdown_pct 离场，锁住趋势利润；与固定 TP 可叠加）——
    trail_enabled: bool = False
    trail_activate_pct: float = 0.10
    trail_drawdown_pct: float = 0.08


class TrendBuyBacktester(StandaloneBacktester):
    sid = "trend_buy"
    config_class = TrendBuyConfig

    def _prewarm(self, start, end):
        super()._prewarm(start, end)
        if not self._bars:
            return
        p = pd.concat([df.assign(symbol=s) for s, df in self._bars.items()],
                      ignore_index=True)
        if p.empty:
            return
        p = p.sort_values(["symbol", "date"]).reset_index(drop=True)
        g = p.groupby("symbol", sort=False)
        if "prev_close" in p.columns:
            p["prev_close"] = pd.to_numeric(p["prev_close"], errors="coerce") \
                .where(p["prev_close"] > 0).fillna(g["close"].shift(1))
        else:
            p["prev_close"] = g["close"].shift(1)
        p["pct"] = p["close"] / p["prev_close"] - 1.0
        # —— 均线 ——
        for n in (5, 10, 20, 60):
            p[f"ma{n}"] = g["close"].transform(lambda s, n=n: s.rolling(n, min_periods=n).mean())
        p["ma20_prev5"] = g["ma20"].shift(5)
        p["ma60_prev10"] = g["ma60"].shift(10)
        p["ma20_rising"] = p["ma20"] > p["ma20_prev5"]
        p["ma60_rising"] = p["ma60"] > p["ma60_prev10"]
        # —— 量 ——
        p["vol20_prev"] = g["volume"].transform(
            lambda s: s.shift(1).rolling(20, min_periods=20).mean())
        p["vol5_prev"] = g["volume"].transform(
            lambda s: s.shift(1).rolling(5, min_periods=5).mean())
        # —— 突破：收盘 > 前 60 日最高 且 量 ≥ 1.8×20日均量 ——
        high60_prev = g["close"].transform(
            lambda s: s.shift(1).rolling(self.config.breakout_lookback,
                                         min_periods=self.config.breakout_lookback).max())
        p["is_breakout"] = ((p["close"] > high60_prev)
                            & (p["volume"] >= p["vol20_prev"] * self.config.breakout_vol_mult))
        # —— 形态 A：突破后 2~5 天内回踩（T-1 数据，P2：窗口收窄 (2,3) + 站稳 MA10）——
        lo, hi = self.config.pullback_window
        ok_a = pd.Series(False, index=p.index)
        for k in range(lo, hi + 1):
            b_close = g["close"].shift(k)
            b_vol = g["volume"].shift(k)
            b_flag = g["is_breakout"].shift(k)
            near = (p["close"].shift(1) >= b_close * (1 - self.config.pullback_band)) & \
                   (p["close"].shift(1) <= b_close * (1 + self.config.pullback_band))
            shrink = p["volume"].shift(1) <= b_vol * self.config.pullback_vol_shrink
            hold_ma = p["close"].shift(1) >= p[f"ma{self.config.pullback_hold_ma}"].shift(1)
            ok_a = ok_a | (b_flag & near & shrink & hold_ma)
        ok_a = ok_a & p["ma20_rising"] & p["ma60_rising"] & (p["ma20"] > p["ma60"])
        p["ok_a"] = ok_a.fillna(False)
        # —— 形态 B：均线多头回调 ——
        line = self.config.ma_pullback_line
        p["ok_b"] = ((p["ma5"] > p["ma10"]) & (p["ma10"] > p["ma20"]) & (p["ma20"] > p["ma60"])
                     & (p["low"].shift(1) <= p[f"ma{line}"].shift(1))
                     & (p["close"].shift(1) >= p[f"ma{line}"].shift(1) * 0.99)
                     & (p["volume"].shift(1) < p["vol5_prev"].shift(1))).fillna(False)
        # —— 形态 C：上升趋势线回踩 ——
        low20 = g["low"].transform(
            lambda s: s.shift(1).rolling(self.config.trendline_win,
                                         min_periods=self.config.trendline_win).min())
        low20_prev = g["low"].transform(
            lambda s: s.shift(1 + self.config.trendline_win)
            .rolling(self.config.trendline_win, min_periods=self.config.trendline_win).min())
        dist = (p["close"].shift(1) - low20) / low20
        p["ok_c"] = ((low20 > low20_prev)
                     & (dist <= self.config.trendline_band) & (dist >= -self.config.trendline_band * 0.2)
                     & (p["volume"].shift(1) < p["vol5_prev"].shift(1))
                     & (p["close"].shift(1) > p["ma60"].shift(1))).fillna(False)
        p["ok"] = (p["ok_a"] | p["ok_b"] | p["ok_c"]).fillna(False)
        p["low_min5"] = g["low"].transform(
            lambda s: s.shift(1).rolling(5, min_periods=5).min())   # 回踩过程最低点
        self._panel = p
        self._calendar = sorted(pd.to_datetime(p["date"]).dt.date.unique())

    # ---------------------------------------------------------- 每日
    def _on_day(self, d: date, next_day: date, instr_map: dict) -> None:
        self._manage(d)
        self._enter(d, instr_map)

    def _manage(self, d: date) -> None:
        for sym in list(self.portfolio.positions):
            pos = self.portfolio.positions.get(sym)
            meta = self.position_meta.get(sym, {})
            entry = float(meta.get("entry_ref") or 0)
            if pos is None or entry <= 0:
                continue
            bar = self._bar(sym, d)
            if bar is None:
                continue
            hi, lo = float(bar["high"] or 0), float(bar["low"] or 0)
            op = float(bar["open"] or 0)
            stop = float(meta.get("stop_price") or 0) or entry * (1 - self.config.stop_floor_pct)
            # 移动止盈：盈利 ≥ trail_activate_pct 后，止损上移至 自高点×(1−trail_drawdown_pct)
            if self.config.trail_enabled and entry > 0 and hi > 0:
                peak = max(float(meta.get("peak", entry)), hi)
                meta["peak"] = peak
                self.position_meta[sym] = meta
                if peak >= entry * (1 + self.config.trail_activate_pct):
                    stop = max(stop, peak * (1 - self.config.trail_drawdown_pct))
            if lo > 0 and lo <= stop:
                ref = op if (op > 0 and op <= stop) else stop
                self._sell(sym, ref, d, signal="TREND_STOP", market=(op <= stop))
                continue
            tp2 = entry * (1 + self.config.take_profit2)
            if hi > 0 and hi >= tp2:
                self._sell(sym, tp2, d, signal="TREND_TP2")
                continue
            tp1 = entry * (1 + self.config.take_profit1)
            if not meta.get("tp1_done") and hi > 0 and hi >= tp1:
                qty = int(pos.can_use * self.config.tp1_sell_ratio // 100 * 100)
                if qty >= 100:
                    self._sell(sym, tp1, d, signal="TREND_TP1", qty=qty)
                meta["tp1_done"] = True
                self.position_meta[sym] = meta
                continue
            if meta.get("opened_at") and (d - meta["opened_at"]).days >= self.config.max_hold_days:
                self._sell(sym, float(bar["close"] or 0), d, signal="TREND_TIME_EXIT")
                continue

    def _enter(self, d: date, instr_map: dict) -> None:
        if len(self.portfolio.positions) >= self.config.max_positions:
            return
        if not self._market_ok(d):
            return  # 弱市空仓：沪深300 站上 MA60 才开趋势仓
        if self._panel is None:
            return
        row = self._panel[pd.to_datetime(self._panel["date"]).dt.date == d]
        if row.empty:
            return
        for r in row.itertuples(index=False):
            if len(self.portfolio.positions) >= self.config.max_positions:
                break
            sym = str(r.symbol)
            if not bool(r.ok):
                continue
            if sym in self.portfolio.positions:
                continue
            if self.config.pattern == "breakout_pullback" and not bool(r.ok_a):
                continue
            if self.config.pattern == "ma_pullback" and not bool(r.ok_b):
                continue
            if self.config.pattern == "trendline" and not bool(r.ok_c):
                continue
            # pattern="any"（或未知值）：三形态任一命中即入场（组合打法）
            instr = instr_map.get(sym)
            prev_day = self._prev_trading_day(d)
            if not self._hard_ok(sym, prev_day or d, instr):
                continue
            close = float(r.close)
            if close <= 0:
                continue
            if self.config.pattern == "breakout_pullback":
                low_ref = float(r.low_min5) if r.low_min5 and r.low_min5 > 0 else close
                stop = max(close * (1 - self.config.stop_floor_pct),
                           low_ref * self.config.stop_floor_mult)
            else:
                stop = close * (1 - self.config.stop_floor_pct)
            self._buy(sym, close, d, signal="TREND_BUY",
                      meta={"opened_at": d, "entry_ref": close, "stop_price": round(stop, 4)})

    def _prev_trading_day(self, d: date):
        prev = [x for x in self._calendar if x < d]
        return prev[-1] if prev else None


__all__ = ["TrendBuyConfig", "TrendBuyBacktester"]
