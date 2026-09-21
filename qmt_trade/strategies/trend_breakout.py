"""趋势突破（策略实验室·方向五）。

公式来源（通达信风格，N1=60 / N2=10 / N3=40）：

    N1:=60; N2:=10; N3:=40;
    ZF12W:(CLOSE/REF(CLOSE,N1)-1)*100;          # 60 日 ≈ 12 周涨幅
    MA10:=MA(CLOSE,10); MA20:=MA(CLOSE,20); MA50:=MA(CLOSE,50);
    TJ1:= ZF12W>30 AND ZF12W<100;               # 中期强势但不过热
    TJ2:= MA10>MA20 AND MA20>MA50;              # 均线多头排列
    TJ3:= MA10>REF(MA10,1) AND MA20>REF(MA20,1);# 短中期均线向上
    HJ:=HHV(HIGH,N3); LD:=LLV(LOW,N3);          # 盘整区间上沿/下沿
    TJ4:= HJ/LD<1.25 AND N3>=N2;                # 区间振幅 <25%，有序回调
    BK:=CLOSE*VOL; BKMA:=MA(BK,5);
    TJ5:= VOL>BKMA*1.2;                         # 放量
    TJ6:= CLOSE>HJ*0.98;                        # 突破盘整上沿
    信号: TJ1 AND TJ2 AND TJ3 AND TJ4 AND TJ5 AND TJ6

实现说明（与原始公式的两处口径处理，均可配置回退）
------------------------------------------------
1) **TJ5 量纲**：原式 ``VOL > MA(CLOSE*VOL,5)*1.2`` 左边是「股数」、右边是
   「金额」，量纲不一致——股价越高条件越不可能成立（50 元股需量能放大约 60
   倍）。默认 ``vol_mode="amount"`` 按 ``BK`` 的语义做放量判定（成交额 >
   5 日均额×1.2）；``vol_mode="share"`` 为原式字面实现（保留用于对照，量化
   量纲问题的影响）；``vol_mode="vol"`` 为纯成交量口径。

2) **HJ 是否含当日**：原公式 ``HHV(HIGH,N3)`` 含当日高点，则 TJ6
   ``CLOSE>HJ*0.98`` 退化为「当日收在当日最高附近」，属自我确认。默认
   ``hhv_include_today=False`` 取**不含当日**的前 N3 日高点，语义才是「突破
   盘整上沿」；置 True 即回到原式字面口径。

3) 原公式只给入场、未给离场。离场规则与 trend_buy 同口径：
   止损 = max(成本×(1−stop_pct), 入场时盘整下沿×stop_low_mult)；
   止盈 +20% 卖一半 / +35% 清仓；时间止损 max_hold_days（默认 20 日）；
   可选移动止盈（trail）与「收盘跌破 MA(ma_fast)」离场。

PIT 纪律：全部指标只用 ≤ T 日收盘数据，T 日**收盘**买入（基类允许收盘买入
用当日 close）。

配置：config/strategies/trend_breakout.yaml
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .base import StandaloneBacktester, StrategyConfig

logger = logging.getLogger("strategies.trend_breakout")


@dataclass
class TrendBreakoutConfig(StrategyConfig):
    # —— 公式原始参数 ——
    n1: int = 60                  # 中期涨幅窗口（60 交易日 ≈ 12 周）
    n2: int = 10                  # 盘整期下限（原式 TJ4 的 N3>=N2，恒真，保留可配）
    n3: int = 40                  # 盘整区间窗口（HHV/LLV）
    zf_low: float = 30.0          # TJ1 涨幅下限(%)
    zf_high: float = 100.0        # TJ1 涨幅上限(%)
    ma_fast: int = 10             # TJ2/TJ3 快线
    ma_mid: int = 20              # TJ2/TJ3 中线
    ma_slow: int = 50             # TJ2 慢线
    range_max: float = 1.25       # TJ4 区间振幅上限（HJ/LD）
    vol_mult: float = 1.2         # TJ5 放量倍数
    vol_mode: str = "amount"      # amount（推荐）/ share（原式字面）/ vol（纯量）
    breakout_mult: float = 0.98   # TJ6 突破上沿的确认比例
    hhv_include_today: bool = False  # False=突破前 N3 日高点（推荐）；True=原式含当日
    # —— 弱市空仓（趋势持仓数周，用 MA60）——
    market_ma_days: int = 60
    # —— 离场 ——
    take_profit1: float = 0.20
    take_profit2: float = 0.35
    tp1_sell_ratio: float = 0.5
    stop_pct: float = 0.08             # 止损兜底：成本 −8%
    stop_low_mult: float = 0.98        # 止损锚点：入场时盘整下沿 ×0.98
    # —— 止损口径（2026-09-21 迭代：8% 兜底让 34% 的仓位 5 日内被打掉）——
    #   pct = max(成本×(1−stop_pct), LD×stop_low_mult)  原始口径
    #   atr = 成本 − atr_mult×ATR(atr_window)           宽窄随个股波动
    #   low = LD×stop_low_mult                          跌破盘整下沿才认输，无百分比兜底
    stop_mode: str = "pct"
    atr_window: int = 20
    atr_mult: float = 2.0
    # 最短持仓天数：>0 时不满该天数不触发止损（除非盘中跌破 min_hold_crash_pct 紧急线）
    min_hold_days: int = 0
    min_hold_crash_pct: float = 0.12
    max_hold_days: int = 20
    ma_exit_enabled: bool = False      # 收盘跌破 MA(ma_fast) 离场
    trail_enabled: bool = False
    trail_activate_pct: float = 0.10
    trail_drawdown_pct: float = 0.08
    # —— 组合 ——
    max_positions: int = 4
    position_fraction: float = 0.25
    rank_by: str = "vol_ratio"    # 候选排序：vol_ratio（放量倍数）/ zf（中期涨幅）
    # —— 入场结构（2026-09-21 第四轮：把「突破当天追」换成「突破后回踩确认」）——
    #   breakout = 突破日收盘直接买（原始公式口径）
    #   pullback = 突破日只登记候选，等 N 日内回踩到突破位附近 + 缩量 + 站稳 MA 才买
    entry_mode: str = "breakout"
    pullback_window: int = 5          # 突破后最多等几个交易日
    pullback_band: float = 0.03       # 回踩到突破日收盘 ±band
    pullback_vol_shrink: float = 0.6  # 回踩日量 ≤ 突破日量 × 该值
    pullback_hold_ma: int = 10        # 回踩日收盘站稳 MA(pullback_hold_ma)
    # —— 其它 ——
    min_list_days: int = 120      # MA50+ZF60 需要足够历史
    exclude_limit_locked: bool = True


class TrendBreakoutBacktester(StandaloneBacktester):
    sid = "trend_breakout"
    config_class = TrendBreakoutConfig

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.signal_log: list[dict] = []      # 每日命中明细（报告用，不进 details）
        self.pending: dict[str, dict] = {}    # entry_mode=pullback：待回踩确认的突破候选

    # ============================================================ 预热：向量化面板
    def _prewarm(self, start, end):
        super()._prewarm(start, end)
        if not self._bars:
            return
        cfg = self.config
        p = pd.concat([df.assign(symbol=s) for s, df in self._bars.items()],
                      ignore_index=True)
        if p.empty:
            return
        p = p.sort_values(["symbol", "date"]).reset_index(drop=True)
        g = p.groupby("symbol", sort=False)
        c = pd.to_numeric(p["close"], errors="coerce")
        hi = pd.to_numeric(p["high"], errors="coerce")
        lo = pd.to_numeric(p["low"], errors="coerce")
        v = pd.to_numeric(p["volume"], errors="coerce")
        amt = (pd.to_numeric(p["amount"], errors="coerce")
               if "amount" in p.columns else c * v)
        p["close"], p["high"], p["low"], p["volume"] = c, hi, lo, v
        p["amount"] = amt.where(amt > 0).fillna(c * v)     # BK ≈ CLOSE*VOL

        # —— TJ1：60 日涨幅 ∈ (30%, 100%) ——
        p["zf"] = (c / g["close"].shift(cfg.n1) - 1.0) * 100.0
        t1 = (p["zf"] > cfg.zf_low) & (p["zf"] < cfg.zf_high)

        # —— TJ2 / TJ3：均线多头排列 + 短中期均线向上 ——
        for n in (cfg.ma_fast, cfg.ma_mid, cfg.ma_slow):
            p[f"ma{n}"] = g["close"].transform(
                lambda s, n=n: s.rolling(n, min_periods=n).mean())
        mf, mm, ms = f"ma{cfg.ma_fast}", f"ma{cfg.ma_mid}", f"ma{cfg.ma_slow}"
        t2 = (p[mf] > p[mm]) & (p[mm] > p[ms])
        gsym = p.groupby("symbol", sort=False)
        t3 = ((p[mf] > gsym[mf].shift(1)) & (p[mm] > gsym[mm].shift(1)))

        # —— TJ4：盘整区间（HJ/LD < 1.25）——
        if cfg.hhv_include_today:
            hj = g["high"].transform(
                lambda s: s.rolling(cfg.n3, min_periods=cfg.n3).max())
            ld = g["low"].transform(
                lambda s: s.rolling(cfg.n3, min_periods=cfg.n3).min())
        else:
            hj = g["high"].transform(
                lambda s: s.shift(1).rolling(cfg.n3, min_periods=cfg.n3).max())
            ld = g["low"].transform(
                lambda s: s.shift(1).rolling(cfg.n3, min_periods=cfg.n3).min())
        p["hj"], p["ld"] = hj, ld
        t4 = ((hj / ld) < cfg.range_max) & (ld > 0) & (hj > 0) \
            & (cfg.n3 >= cfg.n2)

        # —— TJ5：放量（BK=CLOSE*VOL，BKMA=MA(BK,5)）——
        bk = p["amount"]
        p["bkma"] = gsym["amount"].transform(
            lambda s: s.rolling(5, min_periods=5).mean())
        vol_ma5 = gsym["volume"].transform(
            lambda s: s.rolling(5, min_periods=5).mean())
        if cfg.vol_mode == "share":
            t5 = (v > p["bkma"] * cfg.vol_mult)            # 原式字面（量纲混用）
        elif cfg.vol_mode == "vol":
            t5 = (v > vol_ma5 * cfg.vol_mult)
        else:
            t5 = (bk > p["bkma"] * cfg.vol_mult)
        p["vol_ratio"] = bk / p["bkma"].replace(0, np.nan)

        # —— ATR（止损口径 atr 用）——
        pc = gsym["close"].shift(1)
        tr = pd.concat([(hi - lo).abs(), (hi - pc).abs(), (lo - pc).abs()],
                       axis=1).max(axis=1)
        p["atr"] = tr.groupby(p["symbol"], sort=False).transform(
            lambda s: s.rolling(cfg.atr_window, min_periods=cfg.atr_window).mean())

        # —— TJ6：收盘突破盘整上沿 ×0.98 ——
        t6 = (c > hj * cfg.breakout_mult)

        p["signal"] = (t1 & t2 & t3 & t4 & t5 & t6).fillna(False)
        # 分条件命中率（报告/诊断用）
        for k, cond in (("t1", t1), ("t2", t2), ("t3", t3), ("t4", t4),
                        ("t5", t5), ("t6", t6)):
            p[k] = cond.fillna(False)
        self._panel = p
        self._calendar = sorted(pd.to_datetime(p["date"]).dt.date.unique())
        logger.info("趋势突破面板：%d 行 / %d 只，信号 %d 条",
                    len(p), p["symbol"].nunique(), int(p["signal"].sum()))

    # ============================================================ 每日
    def _on_day(self, d: date, next_day: date, instr_map: dict) -> None:
        self._manage(d)
        if self.config.entry_mode == "pullback":
            self._enter_pullback(d, instr_map)
            self._register_signals(d)
        else:
            self._enter(d, instr_map)

    # ---------------------------------------------------------- 离场
    def _manage(self, d: date) -> None:
        cfg = self.config
        for sym in list(self.portfolio.positions):
            pos = self.portfolio.positions.get(sym)
            meta = self.position_meta.get(sym, {})
            entry = float(meta.get("entry_ref") or 0)
            if pos is None or entry <= 0:
                continue
            bar = self._bar(sym, d)
            if bar is None:
                continue
            hi = float(bar["high"] or 0)
            lo = float(bar["low"] or 0)
            op = float(bar["open"] or 0)
            cl = float(bar["close"] or 0)
            stop = float(meta.get("stop_price") or 0) or entry * (1 - cfg.stop_pct)

            # 移动止盈：浮盈达标后，止损上移至 自高点回撤 trail_drawdown_pct
            if cfg.trail_enabled and hi > 0:
                peak = max(float(meta.get("peak", entry)), hi)
                meta["peak"] = peak
                self.position_meta[sym] = meta
                if peak >= entry * (1 + cfg.trail_activate_pct):
                    stop = max(stop, peak * (1 - cfg.trail_drawdown_pct))

            held = (d - meta["opened_at"]).days if meta.get("opened_at") else 999
            crash = entry * (1 - cfg.min_hold_crash_pct)
            too_soon = held < cfg.min_hold_days and not (lo > 0 and lo <= crash)
            if lo > 0 and lo <= stop and not too_soon:
                ref = op if (op > 0 and op <= stop) else stop
                self._sell(sym, ref, d, signal="TB_STOP", market=(op <= stop))
                continue
            tp2 = entry * (1 + cfg.take_profit2)
            if hi > 0 and hi >= tp2:
                self._sell(sym, tp2, d, signal="TB_TP2")
                continue
            tp1 = entry * (1 + cfg.take_profit1)
            if not meta.get("tp1_done") and hi > 0 and hi >= tp1:
                qty = int(pos.can_use * cfg.tp1_sell_ratio // 100 * 100)
                if qty >= 100:
                    self._sell(sym, tp1, d, signal="TB_TP1", qty=qty)
                meta["tp1_done"] = True
                self.position_meta[sym] = meta
                continue
            if cfg.ma_exit_enabled and cl > 0:
                ma_fast = self._panel_value(sym, d, f"ma{cfg.ma_fast}")
                if ma_fast and cl < ma_fast:
                    self._sell(sym, cl, d, signal="TB_MA_EXIT")
                    continue
            if meta.get("opened_at") and (d - meta["opened_at"]).days >= cfg.max_hold_days:
                self._sell(sym, cl, d, signal="TB_TIME_EXIT")
                continue

    # ---------------------------------------------------------- 入场
    def _enter(self, d: date, instr_map: dict) -> None:
        cfg = self.config
        if len(self.portfolio.positions) >= cfg.max_positions:
            return
        if not self._market_ok(d):
            return  # 弱市空仓：沪深300 站上 MA(market_ma_days) 才开趋势仓
        if self._panel is None:
            return
        row = self._panel[pd.to_datetime(self._panel["date"]).dt.date == d]
        if row.empty:
            return
        row = row[row["signal"]]
        if row.empty:
            return
        rank_col = "vol_ratio" if cfg.rank_by == "vol_ratio" else "zf"
        row = row.sort_values(rank_col, ascending=False)
        prev_day = self._prev_trading_day(d)
        for r in row.itertuples(index=False):
            if len(self.portfolio.positions) >= cfg.max_positions:
                break
            sym = str(r.symbol)
            if sym in self.portfolio.positions:
                continue
            instr = instr_map.get(sym)
            if not self._hard_ok(sym, prev_day or d, instr):
                continue
            close = float(r.close)
            if close <= 0:
                continue
            bar = self._bar(sym, d)
            if bar is None:
                continue
            # 一字涨停（最低价≥涨停价）买不进，直接跳过
            if cfg.exclude_limit_locked and self._limit_locked_up(bar, sym, instr):
                continue
            ld = float(r.ld) if r.ld and np.isfinite(r.ld) and r.ld > 0 else close
            stop = self._stop_price(close, ld, self._panel_value(sym, d, "atr"))
            fill = self._buy(sym, close, d, signal="TB_BUY",
                             meta={"opened_at": d, "entry_ref": close,
                                   "stop_price": round(stop, 4),
                                   "hj": round(float(r.hj or 0), 4),
                                   "ld": round(ld, 4)})
            if fill is not None:
                self.signal_log.append({
                    "date": d.isoformat(), "symbol": sym,
                    "close": round(close, 3),
                    "zf": round(float(r.zf), 2),
                    "vol_ratio": round(float(r.vol_ratio or 0), 2),
                    "hj": round(float(r.hj or 0), 3),
                    "ld": round(ld, 3),
                    "stop": round(stop, 3),
                })

    # ---------------------------------------------------------- 入场：回踩确认
    def _day_index(self, d: date) -> int:
        if not hasattr(self, "_day_idx"):
            self._day_idx = {x: i for i, x in enumerate(self._calendar)}
        return self._day_idx.get(d, -1)

    def _register_signals(self, d: date) -> None:
        """``entry_mode=pullback``：当日突破只登记为待回踩候选，不立即买入。"""
        cfg = self.config
        if self._panel is None:
            return
        row = self._panel[(pd.to_datetime(self._panel["date"]).dt.date == d)
                          & (self._panel["signal"])]
        if row.empty:
            return
        idx = self._day_index(d)
        rank_col = "vol_ratio" if cfg.rank_by == "vol_ratio" else "zf"
        for r in row.sort_values(rank_col, ascending=False).itertuples(index=False):
            sym = str(r.symbol)
            if sym in self.portfolio.positions or sym in self.pending:
                continue
            self.pending[sym] = {"date": d, "idx": idx,
                                 "close": float(r.close), "vol": float(r.volume or 0)}

    def _enter_pullback(self, d: date, instr_map: dict) -> None:
        """突破后 N 个交易日内回踩确认买入：回踩到突破位 ±band + 缩量 + 站稳 MA。"""
        cfg = self.config
        if not self.pending or self._panel is None:
            return
        idx = self._day_index(d)
        rows = self._panel[pd.to_datetime(self._panel["date"]).dt.date == d]
        by_sym = {str(r.symbol): r for r in rows.itertuples(index=False)}
        prev_day = self._prev_trading_day(d)
        for sym, info in list(self.pending.items()):
            if idx - info["idx"] > cfg.pullback_window:
                self.pending.pop(sym, None)      # 超过等待窗口仍未回踩 → 作废
                continue
            if len(self.portfolio.positions) >= cfg.max_positions:
                break
            if sym in self.portfolio.positions or not self._market_ok(d):
                continue
            r = by_sym.get(sym)
            if r is None:
                continue
            close = float(r.close)
            if close <= 0:
                continue
            if not (info["close"] * (1 - cfg.pullback_band)
                    <= close <= info["close"] * (1 + cfg.pullback_band)):
                continue
            if info["vol"] > 0 and float(r.volume or 0) > info["vol"] * cfg.pullback_vol_shrink:
                continue
            ma = float(getattr(r, f"ma{cfg.pullback_hold_ma}", 0) or 0)
            if ma > 0 and close < ma:
                continue
            instr = instr_map.get(sym)
            if not self._hard_ok(sym, prev_day or d, instr):
                continue
            bar = self._bar(sym, d)
            if bar is None:
                continue
            if cfg.exclude_limit_locked and self._limit_locked_up(bar, sym, instr):
                continue
            ld = float(getattr(r, "ld", 0) or 0) or close
            stop = self._stop_price(close, ld, self._panel_value(sym, d, "atr"))
            fill = self._buy(sym, close, d, signal="TB_BUY_PB",
                             meta={"opened_at": d, "entry_ref": close,
                                   "stop_price": round(stop, 4),
                                   "hj": round(float(getattr(r, "hj", 0) or 0), 4),
                                   "ld": round(ld, 4)})
            if fill is not None:
                self.pending.pop(sym, None)
                self.signal_log.append({
                    "date": d.isoformat(), "symbol": sym, "close": round(close, 3),
                    "zf": round(float(getattr(r, "zf", 0) or 0), 2),
                    "vol_ratio": round(float(getattr(r, "vol_ratio", 0) or 0), 2),
                    "hj": round(float(getattr(r, "hj", 0) or 0), 3),
                    "ld": round(ld, 3), "stop": round(stop, 3), "entry": "pullback",
                })

    # ============================================================ 工具
    def _stop_price(self, close: float, ld: float, atr: float | None) -> float:
        """按 ``stop_mode`` 计算止损价（入场时一次性确定，之后不再变动）。"""
        cfg = self.config
        if cfg.stop_mode == "atr":
            if atr and np.isfinite(atr) and atr > 0:
                return close - cfg.atr_mult * atr
            return close * (1 - cfg.stop_pct)
        if cfg.stop_mode == "low":
            return ld * cfg.stop_low_mult
        return max(close * (1 - cfg.stop_pct), ld * cfg.stop_low_mult)

    def _panel_value(self, sym: str, d: date, col: str):
        if self._panel is None or col not in self._panel.columns:
            return None
        sub = self._panel[(pd.to_datetime(self._panel["date"]).dt.date == d)
                          & (self._panel["symbol"] == sym)]
        if sub.empty:
            return None
        val = sub.iloc[-1][col]
        return float(val) if val is not None and np.isfinite(val) else None

    def _limit_locked_up(self, bar: dict, sym: str, instr) -> bool:
        """一字涨停：当日最低价 ≥ 涨停价（无法买入）。"""
        limit_up = bar.get("limit_up")
        low = bar.get("low")
        try:
            if limit_up and float(limit_up) > 0 and low is not None:
                return float(low) >= float(limit_up) * 0.999
        except (TypeError, ValueError):
            return False
        return False

    def _prev_trading_day(self, d: date):
        prev = [x for x in self._calendar if x < d]
        return prev[-1] if prev else None


__all__ = ["TrendBreakoutConfig", "TrendBreakoutBacktester"]
