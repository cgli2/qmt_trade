"""尾盘潜伏低吸（策略实验室·方向三，改良版）。

与 tail_pick「当日涨 3%~5% 尾盘追强势」的本质区别：**买回调，不买上涨**。两类形态：

A. 前一日大阳突破趋势（breakout）：
   T-1 收大阳（涨幅 ≥ big_yang_pct）且收盘突破近 20 日新高、站上 MA20（趋势启动）；
   T 尾盘买入。

B. 主线板块分歧低吸（pullback）：
   所属板块近 5 日涨幅排名前 N（主线，不做弱支线）；
   个股自近 5 日高点回撤 pullback_lo~pullback_hi（3%~6% 黄金区）；
   且不破 MA60（不做下跌趋势的低吸）；T 尾盘买入。

离场（T+1 起）：
   止盈1 +take_profit1 卖 tp1_sell_ratio → 止盈2 +take_profit2 清仓；
   硬止损 -stop_pct（盘中 low 触及即离场）；时间止损 max_hold_days 日（收盘卖）；
   若当日收盘涨停则持有（强势延续，不追高但不清仓过早）。

配置：config/settings.yaml::strategies.dip_buy
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .base import StandaloneBacktester, StrategyConfig

logger = logging.getLogger("strategies.dip_buy")


@dataclass
class DipBuyConfig(StrategyConfig):
    # —— 形态 ——
    pattern: str = "both"            # both / breakout / pullback
    # A 大阳突破
    big_yang_pct: float = 0.05       # 前一日涨幅 ≥ 5% 视为大阳
    breakout_window: int = 20        # 突破近 N 日新高
    # B 主线回调
    sector_top_n: int = 5            # 近 5 日板块涨幅前 N（主线）
    sector_win_days: int = 5         # 板块强势窗口
    pullback_lo: float = 0.03        # 自 5 日高点回撤下限
    pullback_hi: float = 0.06        # 自 5 日高点回撤上限
    pullback_win: int = 5            # 回撤参考窗口（近 N 日高点）
    # —— 收紧入场（2026-08-16 P2 迭代：企稳确认，269 笔 -5% 止损是首版最大亏损源）——
    need_stabilize: bool = True      # 回调企稳：当日未破昨低 + 缩量 + 未暴跌
    vol_shrink_max: float = 1.2      # 企稳缩量上限（当日量 ≤ 前5日均量×该值）
    drop_floor: float = 0.02         # 当日跌幅保护（close ≥ 昨收×(1−该值)）
    # —— 离场 ——
    take_profit1: float = 0.08
    take_profit2: float = 0.12
    tp1_sell_ratio: float = 0.5
    # 重构止损：破位才止损 —— max(成本−8% 兜底, 近 N 日最低×0.97)
    stop_floor_pct: float = 0.08
    stop_low_win: int = 3
    stop_low_mult: float = 0.97
    max_hold_days: int = 5
    # —— 其他 ——
    industry_map_path: str = "data/industry_map_em.json"


class DipBuyBacktester(StandaloneBacktester):
    sid = "dip_buy"
    config_class = DipBuyConfig

    def __init__(self, settings, hub, *, initial_cash=1_000_000.0, config=None):
        super().__init__(settings, hub, initial_cash=initial_cash, config=config)
        self._industry_map: dict[str, str] = {}
        self._sector_rank5: dict[date, set[str]] = {}
        self._load_industry_map()

    def _load_industry_map(self):
        path = self.config.industry_map_path
        if not path:
            return
        try:
            import os
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            m = raw.get("map", raw) if isinstance(raw, dict) else {}
            self._industry_map = {str(k): str(v) for k, v in m.items() if v}
        except Exception as exc:  # noqa: BLE001
            logger.warning("行业映射加载失败 %s: %s", path, exc)

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
        # —— 向量化特征（全部用 ≤T-1 数据）——
        p["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
        p["ma60"] = g["close"].transform(lambda s: s.rolling(60, min_periods=60).mean())
        p["vol5_prev"] = g["volume"].transform(
            lambda s: s.shift(1).rolling(5, min_periods=5).mean())
        p["high_prev20"] = g["close"].transform(
            lambda s: s.shift(1).rolling(20, min_periods=20).max())   # 不含当日的前 20 日最高
        p["high5"] = g["close"].transform(
            lambda s: s.shift(1).rolling(5, min_periods=5).max())     # 前 5 日最高
        # A 大阳突破（当日行 = T-1）：大阳 + 突破前20日新高 + 站上 MA20
        p["ok_a"] = ((p["pct"] >= self.config.big_yang_pct)
                     & (p["close"] > p["high_prev20"])
                     & (p["close"] > p["ma20"])).fillna(False)
        # B 主线回调（当日行 = T）：自前5日高点回撤 3%~6% + 站上 MA60
        dd = p["close"] / p["high5"] - 1.0
        ok_b = ((dd <= -self.config.pullback_lo)
                & (dd >= -self.config.pullback_hi)
                & (p["close"] > p["ma60"]))
        # 企稳确认（P2 迭代）：当日未破昨低 + 缩量 + 未暴跌 —— 拦掉"回调未结束继续跌"
        if self.config.need_stabilize:
            prev_low = g["low"].shift(1)
            ok_b = ok_b & (p["close"] >= prev_low) \
                & (p["volume"] <= p["vol5_prev"] * self.config.vol_shrink_max) \
                & (p["close"] >= p["prev_close"] * (1 - self.config.drop_floor))
        p["ok_b"] = ok_b.fillna(False)
        # 近 N 日最低（不含当日）→ 破位止损参考
        p["low_prev"] = g["low"].transform(
            lambda s: s.shift(1).rolling(self.config.stop_low_win,
                                         min_periods=1).min())
        self._panel = p
        self._calendar = sorted(pd.to_datetime(p["date"]).dt.date.unique())
        # 板块近 5 日涨幅 → 每日主线板块集合（向量化）
        w = self.config.sector_win_days
        r5 = p["close"] / g["close"].shift(w) - 1.0
        tmp = pd.DataFrame({"date": pd.to_datetime(p["date"]).dt.date, "symbol": p["symbol"],
                            "ind": p["symbol"].map(self._industry_map), "r5": r5})
        tmp = tmp.dropna(subset=["ind", "r5"])
        self._sector_rank5 = {}
        for d, sub in tmp.groupby("date"):
            agg = sub.groupby("ind")["r5"].agg(["mean", "count"])
            agg = agg[agg["count"] >= 3]
            ranked = agg.sort_values("mean", ascending=False)
            self._sector_rank5[d] = set(ranked.index[: max(0, self.config.sector_top_n)].tolist())

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
            # 破位止损：low 触及 止损价（meta.stop_price = max(成本−8%, 近3日最低×0.97)）
            stop = float(meta.get("stop_price") or 0) or entry * (1 - self.config.stop_floor_pct)
            if lo > 0 and lo <= stop:
                ref = op if (op > 0 and op <= stop) else stop
                self._sell(sym, ref, d, signal="DIP_STOP", market=(op <= stop))
                continue
            # 止盈2：清仓
            tp2 = entry * (1 + self.config.take_profit2)
            if hi > 0 and hi >= tp2:
                self._sell(sym, tp2, d, signal="DIP_TP2")
                continue
            # 止盈1：卖部分
            tp1 = entry * (1 + self.config.take_profit1)
            if not meta.get("tp1_done") and hi > 0 and hi >= tp1:
                qty = int(pos.can_use * self.config.tp1_sell_ratio // 100 * 100)
                if qty >= 100:
                    self._sell(sym, tp1, d, signal="DIP_TP1", qty=qty)
                meta["tp1_done"] = True
                self.position_meta[sym] = meta
                continue
            # 时间止损：收盘卖
            if meta.get("opened_at") and (d - meta["opened_at"]).days >= self.config.max_hold_days:
                self._sell(sym, float(bar["close"] or 0), d, signal="DIP_TIME_EXIT")
                continue

    def _enter(self, d: date, instr_map: dict) -> None:
        if len(self.portfolio.positions) >= self.config.max_positions:
            return
        if not self._market_ok(d):
            return  # 弱市空仓：沪深300 站上 MA20 才低吸
        candidates = self._screen(d, instr_map)
        for sym in candidates:
            if len(self.portfolio.positions) >= self.config.max_positions:
                break
            if sym in self.portfolio.positions:
                continue
            bar = self._bar(sym, d)
            if bar is None or not (bar.get("close") or 0):
                continue
            close = float(bar["close"])
            lu = float(bar.get("limit_up") or 0)
            if lu > 0 and close >= lu * 0.999:
                continue  # 涨停不追
            # 破位止损价：max(成本−8% 兜底, 近3日最低×0.97)
            row = self._panel[pd.to_datetime(self._panel["date"]).dt.date == d]
            low_ref = float(row[row["symbol"] == sym]["low_prev"].iloc[0]) \
                if len(row) and sym in set(row["symbol"]) else close
            stop = max(close * (1 - self.config.stop_floor_pct),
                       low_ref * self.config.stop_low_mult) if low_ref and low_ref > 0 \
                else close * (1 - self.config.stop_floor_pct)
            self._buy(sym, close, d, signal="DIP_BUY",
                      meta={"opened_at": d, "entry_ref": close, "stop_price": round(stop, 4)})

    # ---------------------------------------------------------- 选股（T 收盘决策，买 T 收盘）
    def _screen(self, d: date, instr_map: dict) -> list[str]:
        if self._panel is None:
            return []
        prev_day = self._prev_trading_day(d)
        hot = self._sector_rank5.get(d, set())
        # A 用 T-1 行（大阳突破），B 用 T 行（当日回调）——都在 T 收盘决策，无未来函数
        row_a = self._panel[(pd.to_datetime(self._panel["date"]).dt.date == (prev_day or d))]
        row_b = self._panel[pd.to_datetime(self._panel["date"]).dt.date == d]
        out: list[str] = []
        seen = set()
        for row in row_a.itertuples(index=False):
            sym = str(row.symbol)
            if not bool(row.ok_a):
                continue
            instr = instr_map.get(sym)
            if not self._hard_ok(sym, prev_day or d, instr):
                continue
            out.append(sym)
            seen.add(sym)
        for row in row_b.itertuples(index=False):
            sym = str(row.symbol)
            if sym in seen or not bool(row.ok_b):
                continue
            ind = self._industry_map.get(sym, "")
            if self.config.pattern in ("both", "pullback") and ind and hot and ind not in hot:
                continue
            instr = instr_map.get(sym)
            if not self._hard_ok(sym, d, instr):
                continue
            out.append(sym)
        return out

    def _prev_trading_day(self, d: date):
        prev = [x for x in self._calendar if x < d]
        return prev[-1] if prev else None


__all__ = ["DipBuyConfig", "DipBuyBacktester"]
