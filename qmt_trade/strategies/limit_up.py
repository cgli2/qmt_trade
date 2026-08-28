"""打板策略（策略实验室·方向一）：追涨停板 + 周末/节假日过滤。

核心逻辑（聚宽打板策略的 QMT 复刻）：
* 选股（T-1 收盘后可判定）：昨日涨停 + 连板 ≥ min_boards + 热门板块前 N
  （概念热度代理：行业当日涨幅排名前 N）+ 硬排除。
* 买入（T 日）：开盘涨幅 2%~8% 才买入（T open / T-1 close − 1），
  一字涨停开盘（open ≥ limit_up）买不进，跳过。
* 周五 / 节前最后一天不开新仓（方向一核心改良）：周末两天不确定性
  （外围波动/监管动态/题材分流）会让周一开盘跳空，低开扩大滑点、高开追高风险加大。
* 离场（日线口径）：涨停延续（当日收盘仍涨停）则持有；否则**次日开盘卖出**；
  硬止损 -stop_pct（盘中 low 触及即按止损价/开盘价离场）；时间止损 max_hold_days。

涨停/连板判定口径：
* 优先用数据源 limit_up 列（close ≥ limit_up×0.999）；缺列时按板块涨跌幅阈值
  （主板 10% / 创业板·科创板 20% / 北交所 30% × 0.98）。
* 连板数 = 截至 T-1 收盘的连续涨停天数（含 T-1）。

配置：config/settings.yaml::strategies.limit_up
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..core.instruments import detect_board, normalize_symbol
from .base import StandaloneBacktester, StrategyConfig

logger = logging.getLogger("strategies.limit_up")


@dataclass
class LimitUpConfig(StrategyConfig):
    # —— 打板核心 ——
    min_boards: int = 2            # 连板下限（昨日涨停已含，≥2 = 二板及以上）
    max_boards: int = 0            # 连板上限（0 = 不限）
    open_gap_lo: float = 0.02      # 开盘涨幅下限（相对昨日收盘）
    open_gap_hi: float = 0.08      # 开盘涨幅上限
    hot_sector_top_n: int = 5      # 热门板块前 N（概念热度代理）
    # —— 方向一改良：周末 / 节假日过滤 ——
    exclude_weekend: bool = True   # 周五不开新仓
    exclude_holiday: bool = True   # 节前最后一天不开新仓
    holiday_gap_days: int = 4      # 下一交易日间隔 > 此自然日数视为节前最后一天
    # —— 收紧入场（2026-08-16 P2 迭代）——
    require_non_oneword_prev: bool = True   # 昨日必须是换手板（open < 昨日涨停价，一字板不追）
    # —— 离场（重构止损：破板次日卖为主，硬止损放宽为灾难止损）——
    stop_pct: float = 0.12         # 灾难止损（2026-08-16：0.07→0.12；连板股日内波动大，
                                   #  -7% 盘中止损 23 笔亏 35.7万 是首版最大亏损源，放宽后
                                   #   这些票改由「收盘不涨停次日开盘卖」处理，历史胜率 64%+）
    max_hold_days: int = 5         # 时间止损（持有天数上限）
    # —— 其他 ——
    industry_map_path: str = "data/industry_map_em.json"
    limit_tol: float = 0.999       # 涨停判定容差（相对 limit_up 列）
    board_tol: float = 0.98        # 缺列时按板块涨跌幅×该系数判定涨停


def _board_limit_pct(symbol: str) -> float:
    try:
        b = detect_board(normalize_symbol(symbol)).value
    except Exception:  # noqa: BLE001
        b = "MAIN"
    if b in ("GEM", "STAR"):
        return 0.20
    if b == "BSE":
        return 0.30
    return 0.10


class LimitUpBacktester(StandaloneBacktester):
    """打板/二板族共享引擎（连板参数由 config 决定）。"""

    sid = "limit_up"
    config_class = LimitUpConfig

    def __init__(self, settings, hub, *, initial_cash=1_000_000.0, config=None):
        super().__init__(settings, hub, initial_cash=initial_cash, config=config)
        self._panel: pd.DataFrame | None = None
        self._industry_map: dict[str, str] = {}
        self._sector_rank: dict[date, set[str]] = {}   # day → 热门板块集合
        self._pending_sells: dict[str, str] = {}       # symbol → reason（次日开盘卖）
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

    # ---------------------------------------------------------- 预热增强
    def _prewarm(self, start, end):
        super()._prewarm(start, end)
        if not self._bars:
            return
        p = pd.concat([df.assign(symbol=s) for s, df in self._bars.items()],
                      ignore_index=True)
        if p.empty:
            return
        p = p.sort_values(["symbol", "date"]).reset_index(drop=True)
        g = p.groupby("symbol", sort=False)["close"]
        if "prev_close" in p.columns:
            p["prev_close"] = pd.to_numeric(p["prev_close"], errors="coerce") \
                .where(p["prev_close"] > 0)
            p["prev_close"] = p["prev_close"].fillna(g.shift(1))
        else:
            p["prev_close"] = g.shift(1)
        p["pct"] = p["close"] / p["prev_close"] - 1.0
        if "limit_up" in p.columns:
            lu = pd.to_numeric(p["limit_up"], errors="coerce").where(p["limit_up"] > 0)
            hit = (p["close"] >= lu * self.config.limit_tol).fillna(False)
        else:
            board_pct = p["symbol"].map(_board_limit_pct)
            hit = p["pct"] >= board_pct * self.config.board_tol
        p["hit"] = hit
        # 连板计数（连续涨停天数，含当日）。注意分组键必须用「值变化」而非 (~s).cumsum()，
        # 后者会把 False→True 边界并入同一组导致连板数多 1（2026-08-15 实测修复）。
        p["run"] = p.groupby("symbol", sort=False)["hit"].transform(
            lambda s: s.groupby((s != s.shift()).cumsum()).cumcount() + 1)
        p["boards"] = p["run"].where(p["hit"], 0).astype(int)
        self._panel = p
        self._calendar = sorted(pd.to_datetime(p["date"]).dt.date.unique())
        # 预计算每日热门板块（当日截面：行业涨幅中位数排名前 N）
        for d, sub in p.groupby(p["date"].dt.date):
            pc = pd.to_numeric(sub["prev_close"], errors="coerce")
            cl = pd.to_numeric(sub["close"], errors="coerce")
            valid = (pc > 0) & (cl > 0)
            syms = sub.loc[valid, "symbol"]
            rets = (cl[valid] / pc[valid] - 1.0)
            agg: dict[str, list[float]] = {}
            for s, r in zip(syms, rets):
                ind = self._industry_map.get(str(s), "")
                if ind:
                    agg.setdefault(ind, []).append(float(r))
            ranked = sorted(((float(np.median(v)), k) for k, v in agg.items()
                             if len(v) >= 3), reverse=True)
            self._sector_rank[d] = {k for _, k in ranked[: max(0, self.config.hot_sector_top_n)]}

    def _prev_trading_day(self, d: date) -> date | None:
        prev = [x for x in self._calendar if x < d]
        return prev[-1] if prev else None

    def _next_trading_day(self, d: date) -> date | None:
        nxt = [x for x in self._calendar if x > d]
        return nxt[0] if nxt else None

    # ---------------------------------------------------------- 每日
    def _on_day(self, d: date, next_day: date, instr_map: dict) -> None:
        self._pending_sells = self._exec_pending(d)
        self._manage_holds(d, next_day)
        self._enter(d, next_day, instr_map)

    def _exec_pending(self, d: date) -> dict[str, str]:
        """执行昨日挂起的卖出（今日开盘）。返回仍未执行的挂起（如停牌）。"""
        still: dict[str, str] = {}
        for sym, reason in list(self._pending_sells.items()):
            if sym not in self.portfolio.positions:
                continue
            bar = self._bar(sym, d)
            if bar is None or not (bar.get("open") or 0):
                still[sym] = reason
                continue
            self._sell(sym, float(bar["open"]), d, signal=reason, market=True)
        return still

    def _manage_holds(self, d: date, next_day: date) -> None:
        """持仓管理：硬止损（当日）/ 涨停延续（持有）/ 不涨停或超时（挂起次日开盘卖）。"""
        for sym in list(self.portfolio.positions):
            if sym in self._pending_sells:
                continue  # 今日开盘已卖/待卖
            meta = self.position_meta.get(sym, {})
            entry = float(meta.get("entry_ref") or 0)
            pos = self.portfolio.positions.get(sym)
            if pos is None or entry <= 0:
                continue
            bar = self._bar(sym, d)
            if bar is None:
                continue
            hi, lo = float(bar["high"] or 0), float(bar["low"] or 0)
            op = float(bar["open"] or 0)
            # 硬止损：low 触及成本×(1−stop)
            stop_line = entry * (1 - self.config.stop_pct)
            if lo > 0 and lo <= stop_line:
                ref = op if (op > 0 and op <= stop_line) else stop_line
                self._sell(sym, ref, d, signal="LIMIT_UP_STOP", market=(op <= stop_line))
                continue
            # 时间止损：持有超 max_hold_days → 挂起次日开盘卖
            if meta.get("opened_at") and (d - meta["opened_at"]).days >= self.config.max_hold_days:
                self._pending_sells[sym] = "LIMIT_UP_TIME_EXIT"
                continue
            # 涨停延续判定：当日收盘仍涨停 → 持有；否则挂起次日开盘卖
            close = float(bar["close"] or 0)
            lu = float(bar.get("limit_up") or 0)
            if lu > 0:
                hit = self._is_limit_up(close, lu, tol=self.config.limit_tol)
            else:
                prev = float(bar.get("prev_close") or 0)
                hit = bool(prev > 0 and close > 0
                           and close / prev - 1 >= _board_limit_pct(sym) * self.config.board_tol)
            if hit:
                continue  # 涨停延续 → 持有
            self._pending_sells[sym] = "LIMIT_UP_BREAK"

    def _enter(self, d: date, next_day: date, instr_map: dict) -> None:
        if len(self.portfolio.positions) >= self.config.max_positions:
            return
        if self._blocked_day(d):
            return
        if not self._market_ok(d):
            return  # 弱市空仓：沪深300 站上 MA 才打板
        picks = self._screen(d, instr_map)
        for sym in picks:
            if len(self.portfolio.positions) >= self.config.max_positions:
                break
            if sym in self.portfolio.positions:
                continue
            bar = self._bar(sym, d)
            if bar is None or not (bar.get("open") or 0):
                continue
            op = float(bar["open"])
            prev = float(bar.get("prev_close") or 0) or self._prev_close(sym, d)
            if prev <= 0:
                continue
            gap = op / prev - 1
            if not (self.config.open_gap_lo <= gap <= self.config.open_gap_hi):
                continue
            lu = float(bar.get("limit_up") or 0)
            if lu > 0 and op >= lu * 0.999:
                continue  # 一字涨停开盘，买不进
            self._buy(sym, op, d, signal="LIMIT_UP_BUY",
                      meta={"opened_at": d, "entry_ref": op})

    def _prev_close(self, sym: str, d: date) -> float:
        sub = self._hist(sym)
        if sub is None or sub.empty:
            return 0.0
        sd = sub[sub["date"] < pd.Timestamp(d)]
        return float(sd.iloc[-1]["close"]) if not sd.empty else 0.0

    # ---------------------------------------------------------- 选股
    def _screen(self, d: date, instr_map: dict) -> list[str]:
        if self._panel is None:
            return []
        prev_day = self._prev_trading_day(d)
        if prev_day is None:
            return []
        prev = self._panel[pd.to_datetime(self._panel["date"]).dt.date == prev_day]
        if prev.empty:
            return []
        hot = self._sector_rank.get(prev_day, set())
        out: list[str] = []
        for row in prev.itertuples(index=False):
            sym = str(row.symbol)
            boards = int(row.boards)
            if boards < self.config.min_boards:
                continue
            if self.config.max_boards > 0 and boards > self.config.max_boards:
                continue
            ind = self._industry_map.get(sym, "")
            if self.config.hot_sector_top_n > 0 and hot and ind and ind not in hot:
                continue
            instr = instr_map.get(sym)
            if not self._hard_ok(sym, prev_day, instr):
                continue
            if self.config.require_non_oneword_prev:
                # 昨日换手板（一字板买不进/无真实换手，强度不可持续）
                po = float(row.open or 0)
                plu = float(row.limit_up or 0)
                if plu > 0 and po >= plu * 0.999:
                    continue
            out.append(sym)
        return out

    def _blocked_day(self, d: date) -> bool:
        """方向一改良：周五 / 节前最后一天不开新仓。"""
        if self.config.exclude_weekend and d.weekday() == 4:
            return True
        if self.config.exclude_holiday:
            nxt = self._next_trading_day(d)
            if nxt and (nxt - d).days > self.config.holiday_gap_days:
                return True
        return False


__all__ = ["LimitUpConfig", "LimitUpBacktester"]
