"""红利低波（高股息 + 低波动）选股篮子策略（策略实验室·方向六）。

为什么是"红利低波"：
--------------
在 A 股近年（2019-2026）最稳的几类因子里，红利（高股息）与低波动长期、稳定地
跑赢宽基，且两者之间低相关、组合后回撤更小。对普通投资者而言，它也是最易解释、
最易落地的"防守型底仓"：买的不是故事，是能持续分红、价格不怎么上蹿下跳的蓝筹。

数据忠诚度的诚实说明（重要）：
----------------------------
本框架的 ``DataHub`` **没有 point-in-time 的股息率/分红率字段**（``Fundamental``
仅有 ``eps``/``bps``/``roe`` 等，``EventCategory.DIVIDEND`` 只给事件文本）。
因此"红利"无法被字面数据化。我们采用学术界与业界公认的**经济代理**：

    * 红利股 ≈ 被低估的成熟价值股 → 用估值因子代理：
        - earnings_yield = EPS / 收盘价        （PE 倒数，越大越便宜）
        - book_to_price   = BPS / 收盘价        （PB 倒数，越大越便宜）
      两者都来自已公告财报（按 ann_date 切片，PIT 干净），用当日收盘价现算，
      不取数据源的"最新 PE/PB 快照"（那种一用就穿越）。
    * 低波 = 过去 N 日收益率的已实现波动率（年化），越低越好。

综合分 = value_weight × 价值分 + lowvol_weight × 低波分，选前 N 只。
价值分与低波分各自在候选池内做**分位排名**（0~1，越大越好），对量纲与离群稳健。

**数据可得性约束（2026-09-21 实测）**：本机 QMT mini 客户端与 akshare provider 都只
缓存**最近 1~3 个季度**的财务快照，2020-2022 的历史 EPS/BPS 本地取不到。因此：

    * ``value_source="fundamental"``（基本面价值，最忠实红利代理）只在财务数据
      可得的近期窗口有效；历史窗口无财务则自动退化为"仅低波"。
    * ``value_source="price"``（默认，跨窗口一致可回测）：价值腿改用**价格代理**
      —— 过去 ``price_value_window`` 日收益率的相反数做分位排名（逆向价值 tilt，
      红利股多为低动量、低估值、不怎么涨的成熟股）。该模式不依赖任何财务数据，
      可在全部窗口（含 2022 熊市 / 2020-2021 牛市两个从未检视窗口）一致回测，
      是严格样本外验证的主口径。

调仓：季度（默认）或月度再平衡，等权（默认）或低波加权（inverse-vol）。
    弱市闸门默认关闭——红利低波是"始终在场、定期再平衡"的防守底仓，不该在弱市空仓
    （那会把回撤最大的时段留给现金，与防守诉求相悖）。可作为敏感性开关验证。

PIT 纪律：
    * 估值用 ``get_latest_fundamentals(syms, asof=d)``，按公告日切片，≤ d 可见。
    * 低波用 ≤ d 收盘的日线现算。
    * 调仓日 T 收盘决策、T 收盘买入（基类允许 T 日收盘买入用当日 close），无前视。

配置：config/strategies/dividend_low_vol.yaml
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .base import StandaloneBacktester, StrategyConfig

logger = logging.getLogger("strategies.dividend_low_vol")


@dataclass
class DividendLowVolConfig(StrategyConfig):
    # —— 选股因子 ——
    vol_window: int = 60                  # 低波：过去 N 日收益波动率（年化）
    value_weight: float = 0.5             # 价值分权重（红利代理）
    lowvol_weight: float = 0.5            # 低波分权重
    value_source: str = "price"           # price（跨窗口可回测，默认）/ fundamental（需财务数据）
    price_value_window: int = 120         # 价格价值代理：回溯 N 日收益率（逆向价值 tilt）
    require_positive_eps: bool = True      # 仅 fundamental 模式：亏损股（EPS<=0）无估值意义
    # —— 篮子结构 ——
    max_positions: int = 20                # 篮子只数
    position_fraction: float = 0.05        # 仅用于参数面板兼容（实际按 equity×weight 建仓）
    rebalance_period: int = 3             # 3=季度，1=月度
    weight_mode: str = "equal"             # equal（等权）/ inv_vol（低波加权）
    # —— 弱市闸门（红利低波默认不空仓，stay invested）——
    market_filter_enabled: bool = False
    market_ma_days: int = 60
    market_ma_days2: int = 0
    # —— 持仓门槛 ——
    min_list_days: int = 250               # 红利股都是上市够久的成熟公司
    exclude_st: bool = True
    exclude_suspended: bool = True
    exclude_limit_locked: bool = True
    allowed_boards: list[str] = None       # type: ignore[assignment]
    # —— 预热（需覆盖 vol_window + 缓冲）——
    warmup_days: int = 260

    def __post_init__(self):
        if self.allowed_boards is None:
            self.allowed_boards = ["MAIN", "GEM", "STAR"]


class DividendLowVolBacktester(StandaloneBacktester):
    sid = "dividend_low_vol"
    config_class = DividendLowVolConfig

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.signal_log: list[dict] = []           # 每次调仓的选股明细（报告用）
        self._vol_lookup: dict[tuple[str, date], float] = {}
        self._rebal_days: set[date] = set()
        self._price_cache: dict[date, dict[str, float]] = {}
        self._last_target: list[tuple[str, float]] = []

    # ============================================================ 预热
    def _prewarm(self, start: date, end: date) -> None:
        super()._prewarm(start, end)
        self._build_vol_panel()
        self._build_rebal_days(start, end)

    def _build_vol_panel(self) -> None:
        """全市场日线拼成面板，用 groupby.rolling 一次算完各标的已实现波动率。"""
        if not self._bars:
            return
        frames = []
        for sym, sub in self._bars.items():
            if sub is None or sub.empty or "close" not in sub.columns:
                continue
            g = pd.DataFrame({"date": sub["date"], "close": sub["close"].astype(float)})
            g["symbol"] = sym
            frames.append(g)
        if not frames:
            return
        panel = pd.concat(frames, ignore_index=True)
        panel["date"] = pd.to_datetime(panel["date"]).dt.date
        panel["ret"] = panel.groupby("symbol")["close"].pct_change()
        vol = (panel.groupby("symbol")["ret"]
                    .transform(lambda s: s.rolling(self.config.vol_window).std())
                    * math.sqrt(252))
        panel["vol"] = vol
        panel = panel.dropna(subset=["vol"])
        self._vol_lookup = {
            (sym, d): float(v)
            for sym, d, v in zip(panel["symbol"], panel["date"], panel["vol"])
        }
        logger.info("红利低波：波动率面板就绪，标的 %d，记录 %d",
                    panel["symbol"].nunique(), len(self._vol_lookup))

    def _build_rebal_days(self, start: date, end: date) -> None:
        days = self._trading_days(start, end)
        if not days:
            return
        period = self.config.rebalance_period
        self._rebal_days = {days[0]}        # 首日建仓
        for i in range(1, len(days)):
            prev, cur = days[i - 1], days[i]
            if self._boundary_crossed(prev, cur, period):
                self._rebal_days.add(prev)  # 该周期最后一个交易日
        logger.info("红利低波：调仓日 %d 个（周期=%d月）", len(self._rebal_days), period)

    @staticmethod
    def _boundary_crossed(prev: date, cur: date, period: int) -> bool:
        if period <= 1:
            return (prev.year, prev.month) != (cur.year, cur.month)
        # 季度（period==3）：年或季度变化即越界
        return (prev.year != cur.year) or ((prev.month - 1) // 3 != (cur.month - 1) // 3)

    # ============================================================ 主日循环
    def _on_day(self, d: date, next_day: date, instr_map: dict) -> None:
        if d not in self._rebal_days:
            return
        if not self._market_ok(d):
            return
        target = self._select(d, instr_map)
        if not target:
            logger.info("%s 调仓日 %s 无合格标的，保持现金", self.sid, d)
            return
        self._rebalance(d, target)

    # ============================================================ 选股
    def _prices_on(self, d: date) -> dict[str, float]:
        """当日收盘价查表（按标的缓存：每个调仓日只扫一次全市场）。"""
        cached = self._price_cache.get(d)
        if cached is not None:
            return cached
        out: dict[str, float] = {}
        for sym, sub in self._bars.items():
            if sub is None or sub.empty:
                continue
            m = sub["date"] == pd.Timestamp(d)
            if not m.any():
                continue
            try:
                out[sym] = float(sub.loc[m, "close"].iloc[-1])
            except (TypeError, ValueError, IndexError):
                continue
        self._price_cache[d] = out
        return out

    def _trailing_return(self, sym: str, d: date, window: int) -> float | None:
        """sym 在 d 之前 window 个交易日的累计收益率（价格价值代理用）。"""
        sub = self._hist(sym)
        if sub is None or sub.empty:
            return None
        sd = sub[sub["date"] <= pd.Timestamp(d)]
        if len(sd) <= window:
            return None
        try:
            past = float(sd.iloc[-(window + 1)]["close"])
            now = float(sd.iloc[-1]["close"])
        except (TypeError, ValueError, IndexError):
            return None
        if past <= 0 or now <= 0:
            return None
        return now / past - 1.0

    def _select(self, d: date, instr_map: dict) -> list[tuple[str, float]]:
        """返回 [(symbol, weight), ...]，按综合分取前 max_positions 只。"""
        cfg = self.config
        close_on = self._prices_on(d)
        data: dict[str, dict] = {}
        for sym in self.universe:
            bar = self._bar(sym, d)
            if bar is None:
                continue
            if cfg.exclude_suspended and bool(bar.get("is_suspended", False)):
                continue
            instr = instr_map.get(sym)
            if not self._hard_ok(sym, d, instr):
                continue
            close = float(bar["close"])
            vol = self._vol_lookup.get((sym, d))
            if vol is None or not np.isfinite(vol) or vol <= 0:
                continue
            data[sym] = {"close": close, "vol": vol}
        if not data:
            return []

        def pct_rank(x: np.ndarray) -> np.ndarray:
            return pd.Series(x).rank(pct=True, method="average").to_numpy()

        syms = list(data)
        if cfg.value_source == "fundamental":
            # —— 价值腿用 PIT 财报的 eps/bps（最忠实红利代理）——
            try:
                frecs = self.hub.get_latest_fundamentals(syms, asof=d)
            except Exception as exc:  # noqa: BLE001 - 财务源断连时降级为空仓
                logger.warning("%s %s 取财务失败（降级空仓）: %s", self.sid, d, exc)
                return []
            rows = []
            for sym in syms:
                f = frecs.get(sym)
                if f is None:
                    continue
                eps = getattr(f, "eps", np.nan)
                bps = getattr(f, "bps", np.nan)
                if cfg.require_positive_eps and not (eps and eps > 0):
                    continue
                if not (bps and bps > 0):
                    continue
                ew = float(eps) / data[sym]["close"]
                bp = float(bps) / data[sym]["close"]
                rows.append((sym, ew, bp, data[sym]["vol"]))
            if not rows:
                return []
            sym_a = np.array([r[0] for r in rows], dtype=object)
            vol_a = np.array([r[3] for r in rows], dtype=float)
            ew_a = np.array([r[1] for r in rows], dtype=float)
            bp_a = np.array([r[2] for r in rows], dtype=float)
            value_score = (pct_rank(ew_a) + pct_rank(bp_a)) / 2.0   # 越大越便宜
            n_value = len(rows)
        else:
            # —— price 模式：价值腿用逆向价值 tilt（过去 window 日收益率为负者更优）——
            rets = []
            for sym in syms:
                tr = self._trailing_return(sym, d, cfg.price_value_window)
                rets.append(tr if tr is not None else np.nan)
            ret_a = np.array(rets, dtype=float)
            sym_a = np.array(syms, dtype=object)
            vol_a = np.array([data[s]["vol"] for s in syms], dtype=float)
            valid = np.isfinite(ret_a)
            rv = ret_a.copy()
            rv[~valid] = np.nan
            value_score_full = pct_rank(rv)
            value_score = np.where(valid, value_score_full, 0.5)     # 无历史者居中
            n_value = int(valid.sum())

        lowvol_score = 1.0 - pct_rank(vol_a)                        # 低波：波动越小越高
        composite = cfg.value_weight * value_score + cfg.lowvol_weight * lowvol_score

        order = np.argsort(-composite)[: cfg.max_positions]
        chosen = [(str(sym_a[i]), float(composite[i]),
                   float(vol_a[i])) for i in order]

        # —— 权重 ——
        if cfg.weight_mode == "inv_vol":
            inv = np.array([1.0 / v for _, _, v in chosen])
            w = inv / inv.sum()
        else:  # equal
            w = np.full(len(chosen), 1.0 / len(chosen))
        target = [(sym, float(wt)) for (sym, _, _), wt in zip(chosen, w)]

        self.signal_log.append({
            "date": d.isoformat(), "n_candidates": n_value,
            "n_selected": len(target), "value_source": cfg.value_source,
            "selected": [{"symbol": s, "weight": wt} for s, wt in target],
        })
        self._last_target = target
        logger.info("%s %s 选股 %d/%d（%s），权重=%s",
                    self.sid, d, len(target), n_value, cfg.value_source, cfg.weight_mode)
        return target

    # ============================================================ 调仓执行
    def _rebalance(self, d: date, target: list[tuple[str, float]]) -> None:
        cfg = self.config
        # 1) 清掉不在新篮子里的旧仓（全量再平衡）
        for sym in list(self.portfolio.positions):
            px = self._close_px(sym, d)
            if px and px > 0:
                self._sell(sym, px, d, signal="DLV_SELL")
        # 2) 按目标权重建仓
        equity = self.portfolio.total_asset
        for sym, w in target:
            px = self._close_px(sym, d)
            if px and px > 0:
                self._buy(sym, px, d, notional=equity * w, signal="DLV_BUY")
