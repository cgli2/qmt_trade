"""市场状态识别（Regime）—— 设计 6.2.2。

这是 qmt_etf 和 TradingAgents-CN 都没有的模块，但它是整个系统里
**性价比最高的风险控制**：在 2015/2018/2024-01 这种系统性下跌里，
个股选得再好也没用，唯一有效的动作是降总仓位。

四态判定（设计表格）：

====================  ==============  ============  ==================
Regime                总仓位上限      新开仓限制    典型特征
====================  ==============  ============  ==================
TREND_UP              80%             正常          站上 20/60 日线，宽度健康
RANGE                 50%             仅高分标的    震荡无方向
TREND_DOWN            20%             仅事件驱动    指数破位，宽度恶化
RISK_OFF              0%（只平不开）  禁止          极端波动/系统性风险
====================  ==============  ============  ==================

设计文档里标了【待确认】"阈值需用历史数据标定"。这里的做法是：
把所有阈值放进 ``settings.regime.*``，代码里只写默认值，
标定完直接改 YAML，不用动代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from ..core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from ..core.config import Settings
    from ..datahub.manager import DataHub

logger = get_logger("features.regime")


class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    RANGE = "RANGE"
    TREND_DOWN = "TREND_DOWN"
    RISK_OFF = "RISK_OFF"

    @property
    def allow_new_position(self) -> bool:
        return self is not Regime.RISK_OFF


#: 各状态的默认总仓位上限（可被 settings.regime.max_position 覆盖）
DEFAULT_MAX_POSITION = {
    Regime.TREND_UP: 0.80,
    Regime.RANGE: 0.50,
    Regime.TREND_DOWN: 0.20,
    Regime.RISK_OFF: 0.00,
}

#: 各状态下新开仓的**截面分位**门槛（0~1）：只有综合分排在全市场前 (1-p) 的票才准开仓。
#:
#: ⚠ 这里必须用"截面分位"而不是"绝对分数"。综合分 = Σ(因子分位 × 权重)，
#: 单因子分位在 [0,1] 均匀分布、均值 0.5，几十个因子加权平均之后被中心极限定理
#: 死死拽向 0.5 —— 实测 30+ 因子时全市场综合分区间只有 [0.0, 0.67]，中位数 0.496。
#: 早期版本这里写的是绝对分 0.70/0.85/0.95，结果是**任何行情下候选池都是空的**，
#: 而且不报错，只是"今天没有符合条件的票"，极难察觉。
#: 分位门槛对分布形状不敏感，无论因子怎么增减都能稳定表达"只要最好的那一撮"。
DEFAULT_MIN_PERCENTILE = {
    Regime.TREND_UP: 0.50,   # 上涨趋势：一半以上的票都可考虑
    Regime.RANGE: 0.70,      # 震荡：只要前 30%
    Regime.TREND_DOWN: 0.90,  # 下跌趋势：只要前 10%，且总仓位另有 20% 上限
    Regime.RISK_OFF: 1.01,   # >1 即永不满足，等价于禁止开仓
}

#: 绝对分底线（可选的**额外**约束，默认 0 = 不生效）。
#: 用途：全市场都很烂时，分位门槛照样能选出"矮子里的高个"，绝对底线可以一票否决。
#: 需要启用时在 settings.regime.min_score 里按 Regime 配置，建议不超过 0.55。
DEFAULT_MIN_SCORE = {
    Regime.TREND_UP: 0.0,
    Regime.RANGE: 0.0,
    Regime.TREND_DOWN: 0.0,
    Regime.RISK_OFF: 1.01,
}


@dataclass
class RegimeSnapshot:
    """一次 Regime 判定的完整结果，落库用于复盘归因。"""

    asof: date
    regime: Regime
    max_position: float
    #: 绝对分底线（默认 0 = 不生效），语义见 DEFAULT_MIN_SCORE
    min_score: float
    #: 截面分位门槛（主门槛），语义见 DEFAULT_MIN_PERCENTILE
    min_percentile: float = 0.0
    #: 各维度打分，-1（极空）~ +1（极多）
    scores: dict[str, float] = field(default_factory=dict)
    #: 原始指标值，用于人工核对
    metrics: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    #: 数据不足时降级为 RANGE 并标记，下游据此可选择更保守策略
    degraded: bool = False

    def to_dict(self) -> dict:
        return {
            "asof": self.asof.isoformat(),
            "regime": self.regime.value,
            "max_position": self.max_position,
            "min_score": self.min_score,
            "min_percentile": self.min_percentile,
            "scores": self.scores,
            "metrics": self.metrics,
            "reason": self.reason,
            "degraded": self.degraded,
        }


class RegimeDetector:
    """基于指数结构 + 市场宽度 + 波动率的多维打分判定。

    不用单一指标（比如"跌破 60 日线就空仓"）是因为那种规则在震荡市会被反复打脸，
    实测年化换手能到 20 次以上。多维加权 + 迟滞（hysteresis）能显著减少抖动。
    """

    def __init__(self, settings: "Settings", hub: "DataHub"):
        self.settings = settings
        self.hub = hub
        cfg = settings.section("regime") or {}

        def _pick(*keys: str, default: float) -> float:
            """兼容新旧配置键名：依次尝试，取第一个存在的值。
            2026-08-12（Phase 2）：settings.yaml 曾写 drawdown_risk_off/vol_risk_off
            而代码读 drawdown_riskoff/vol_extreme，键名不匹配导致配置从未生效。"""
            for k in keys:
                if k in cfg:
                    return float(cfg[k])
            return default

        self.index_symbol = cfg.get("index_symbol", "000300.SH")
        self.second_index = cfg.get("second_index", "399006.SZ")
        self.lookback = int(cfg.get("lookback_days", 250))
        self.vol_window = int(cfg.get("vol_window", 20))
        self.vol_extreme = _pick("vol_extreme", "vol_risk_off", default=0.035)  # 日波动率 3.5% 以上视为极端
        self.vol_high = float(cfg.get("vol_high", 0.022))
        self.drawdown_riskoff = _pick("drawdown_riskoff", "drawdown_risk_off", default=-0.12)  # 20 日回撤超此值 → RISK_OFF
        # Phase 2（2026-08-12）：硬性降仓阈值 —— 指数跌破 60 日线 且 20日回撤 ≤ 此值
        # → 强制 TREND_DOWN（20% 仓位）。修复"下跌市 80% 满仓穿越"（V0~V4 真实数据确认的亏钱主因）。
        self.drawdown_trenddown = float(cfg.get("drawdown_trenddown", -0.06))
        self.breadth_good = float(cfg.get("breadth_good", 0.55))
        self.breadth_bad = float(cfg.get("breadth_bad", 0.40))
        self.up_threshold = float(cfg.get("up_threshold", 0.35))
        self.down_threshold = float(cfg.get("down_threshold", -0.30))
        self.hysteresis = float(cfg.get("hysteresis", 0.10))
        overrides = cfg.get("max_position") or cfg.get("position_cap") or {}
        self.max_position = {
            r: float(overrides.get(r.value, DEFAULT_MAX_POSITION[r])) for r in Regime
        }
        score_overrides = cfg.get("min_score") or {}
        self.min_score = {
            r: float(score_overrides.get(r.value, DEFAULT_MIN_SCORE[r])) for r in Regime
        }
        pct_overrides = cfg.get("min_percentile") or {}
        self.min_percentile = {
            r: float(pct_overrides.get(r.value, DEFAULT_MIN_PERCENTILE[r])) for r in Regime
        }
        self._last: Regime | None = None
        # 指数行情缓存：回测中指数历史不可变，按标的一次性拉全量并本地切片，
        # 避免每次 detect 用滑动起点 (day-lookback*1.6) 导致 get_index_bars 缓存键
        # 每天变化、沪深300/创业板指每天重复下载。
        self._idx_cache: dict[str, pd.DataFrame] = {}
        # 固定历史起点（由 pipeline.run 的 history_start 下传）：用于指数一次性拉取。
        self.fixed_start: date | None = None

    # ------------------------------------------------------------ 主入口
    def detect(
        self,
        asof: date | datetime,
        *,
        panel: pd.DataFrame | None = None,
    ) -> RegimeSnapshot:
        """判定市场状态。

        ``panel`` 是全市场行情长表（用于算宽度）。不传则跳过宽度维度，
        只用指数结构和波动率 —— 精度下降但不会崩，符合 P4。
        """
        day = asof.date() if isinstance(asof, datetime) else asof
        metrics: dict[str, float] = {}
        scores: dict[str, float] = {}
        degraded = False

        idx = self._load_index(self.index_symbol, day)
        if idx is None or len(idx) < 60:
            logger.warning("指数数据不足（%s），Regime 降级为 RANGE", self.index_symbol)
            return self._make(day, Regime.RANGE, scores, metrics, "指数数据不足，保守降级", True)

        trend_score, tm = self._trend_score(idx)
        scores["trend"] = trend_score
        metrics.update(tm)

        vol_score, vm = self._volatility_score(idx)
        scores["volatility"] = vol_score
        metrics.update(vm)

        if panel is not None and not panel.empty:
            breadth_score, bm = self._breadth_score(panel, day)
            scores["breadth"] = breadth_score
            metrics.update(bm)
        else:
            degraded = True

        second = self._load_index(self.second_index, day)
        if second is not None and len(second) >= 60:
            s2, _ = self._trend_score(second)
            scores["trend_gem"] = s2

        # ---- 加权合成 ----
        weights = {"trend": 0.40, "breadth": 0.25, "volatility": 0.20, "trend_gem": 0.15}
        total_w = sum(weights[k] for k in scores if k in weights)
        composite = (
            sum(scores[k] * weights[k] for k in scores if k in weights) / total_w
            if total_w > 0
            else 0.0
        )
        metrics["composite"] = round(composite, 4)

        # ---- 硬性 RISK_OFF 条件：任一触发直接空仓，不参与加权 ----
        hard_reasons = []
        if metrics.get("vol_20d", 0) >= self.vol_extreme:
            hard_reasons.append(f"日波动率 {metrics['vol_20d']:.2%} ≥ {self.vol_extreme:.2%}")
        if metrics.get("dd_20d", 0) <= self.drawdown_riskoff:
            hard_reasons.append(f"20 日回撤 {metrics['dd_20d']:.2%} ≤ {self.drawdown_riskoff:.2%}")
        if hard_reasons:
            return self._make(day, Regime.RISK_OFF, scores, metrics, "; ".join(hard_reasons), degraded)

        # ---- 硬性降仓（TREND_DOWN）：指数跌破 60 日线 且 20 日回撤超阈值 ----
        # Phase 2（2026-08-12）：RISK_OFF 阈值之下的"阴跌"阶段同样危险 ——
        # 此前 08-11 判 TREND_UP、满仓 80% 穿越 -10% 回撤的下跌市，就是缺这道熔断。
        # 用结构性信号（跌破 60 日线）叠加回撤确认，避免震荡市边界误伤。
        dd_20d = metrics.get("dd_20d", 0)
        ma60 = metrics.get("ma60", 0)
        idx_close = metrics.get("index_close", 0)
        if (ma60 > 0 and idx_close > 0 and idx_close < ma60
                and dd_20d <= self.drawdown_trenddown):
            hard_reasons.append(
                f"跌破60日线({idx_close:.0f}<{ma60:.0f})且20日回撤 {dd_20d:.2%} "
                f"≤ {self.drawdown_trenddown:.2%}"
            )
            return self._make(day, Regime.TREND_DOWN, scores, metrics,
                              "; ".join(hard_reasons), degraded)

        regime = self._classify(composite)
        reason = (
            f"综合分 {composite:+.2f}（趋势 {scores.get('trend', 0):+.2f}/"
            f"宽度 {scores.get('breadth', 0):+.2f}/波动 {scores.get('volatility', 0):+.2f}）"
        )
        return self._make(day, regime, scores, metrics, reason, degraded)

    # ------------------------------------------------------------ 分维度打分
    def _trend_score(self, idx: pd.DataFrame) -> tuple[float, dict]:
        close = idx["close"]
        ma20 = close.rolling(20, min_periods=20).mean()
        ma60 = close.rolling(60, min_periods=60).mean()
        last, m20, m60 = float(close.iloc[-1]), float(ma20.iloc[-1]), float(ma60.iloc[-1])
        parts = []
        parts.append(1.0 if last > m20 else -1.0)
        parts.append(1.0 if last > m60 else -1.0)
        parts.append(1.0 if m20 > m60 else -1.0)
        # 均线斜率：20 日线过去 10 天的变化率
        slope = (m20 / float(ma20.iloc[-11]) - 1) if len(ma20.dropna()) > 11 else 0.0
        parts.append(float(np.clip(slope * 50, -1, 1)))
        metrics = {
            "index_close": round(last, 2),
            "ma20": round(m20, 2),
            "ma60": round(m60, 2),
            "ma20_slope": round(slope, 5),
        }
        return float(np.mean(parts)), metrics

    def _volatility_score(self, idx: pd.DataFrame) -> tuple[float, dict]:
        ret = idx["close"].pct_change()
        vol = float(ret.rolling(self.vol_window, min_periods=10).std().iloc[-1] or 0)
        roll_max = idx["close"].rolling(20, min_periods=5).max()
        dd = float((idx["close"].iloc[-1] / roll_max.iloc[-1]) - 1)
        # 波动越高分越低；低于 vol_high 时给正分
        if vol >= self.vol_extreme:
            score = -1.0
        elif vol >= self.vol_high:
            score = -(vol - self.vol_high) / max(1e-9, self.vol_extreme - self.vol_high)
        else:
            score = 1.0 - vol / max(1e-9, self.vol_high)
        return float(np.clip(score, -1, 1)), {"vol_20d": round(vol, 5), "dd_20d": round(dd, 5)}

    def _breadth_score(self, panel: pd.DataFrame, day: date) -> tuple[float, dict]:
        """市场宽度：上涨家数占比 + 站上 20 日线家数占比 + 涨跌停比。"""
        df = panel.copy()
        df["date"] = pd.to_datetime(df["date"])
        recent = df[df["date"].dt.date <= day]
        if recent.empty:
            return 0.0, {}
        last_day = recent["date"].max()
        today = recent[recent["date"] == last_day]
        if today.empty:
            return 0.0, {}

        chg = (today["close"] / today["prev_close"].replace(0, np.nan) - 1).dropna()
        up_ratio = float((chg > 0).mean()) if len(chg) else 0.5

        ma20 = (
            recent.sort_values("date")
            .groupby("symbol", sort=False)["close"]
            .apply(lambda s: s.tail(20).mean() if len(s) >= 20 else np.nan)
        )
        last_close = today.set_index("symbol")["close"]
        common = last_close.index.intersection(ma20.index)
        above = (
            float((last_close.loc[common] > ma20.loc[common]).mean())
            if len(common)
            else 0.5
        )

        n_up = int((today["close"] >= today.get("limit_up", np.inf) - 1e-6).sum())
        n_down = int((today["close"] <= today.get("limit_down", -np.inf) + 1e-6).sum())
        lu_ratio = (n_up - n_down) / max(1, n_up + n_down) if (n_up + n_down) else 0.0

        def norm(x: float) -> float:
            """把 0~1 的占比映射到 -1~+1，以 breadth_bad/good 为参考锚点。"""
            if x >= self.breadth_good:
                return min(1.0, (x - self.breadth_good) / max(1e-9, 1 - self.breadth_good))
            if x <= self.breadth_bad:
                return max(-1.0, -(self.breadth_bad - x) / max(1e-9, self.breadth_bad))
            return 0.0

        score = 0.4 * norm(up_ratio) + 0.4 * norm(above) + 0.2 * float(np.clip(lu_ratio, -1, 1))
        return float(np.clip(score, -1, 1)), {
            "breadth_up_ratio": round(up_ratio, 4),
            "breadth_above_ma20": round(above, 4),
            "limit_up_count": n_up,
            "limit_down_count": n_down,
        }

    # ------------------------------------------------------------ 分类与迟滞
    def _classify(self, composite: float) -> Regime:
        up, down = self.up_threshold, self.down_threshold
        # 迟滞：已处于某状态时，需多走 hysteresis 的幅度才切出去，避免边界反复横跳
        if self._last is Regime.TREND_UP:
            up -= self.hysteresis
        elif self._last is Regime.TREND_DOWN:
            down += self.hysteresis
        if composite >= up:
            return Regime.TREND_UP
        if composite <= down:
            return Regime.TREND_DOWN
        return Regime.RANGE

    def _make(
        self,
        day: date,
        regime: Regime,
        scores: dict,
        metrics: dict,
        reason: str,
        degraded: bool,
    ) -> RegimeSnapshot:
        self._last = regime
        snap = RegimeSnapshot(
            asof=day,
            regime=regime,
            max_position=self.max_position[regime],
            min_score=self.min_score[regime],
            min_percentile=self.min_percentile[regime],
            scores={k: round(v, 4) for k, v in scores.items()},
            metrics=metrics,
            reason=reason,
            degraded=degraded,
        )
        logger.info(
            "Regime[%s] = %s 上限%.0f%% | %s",
            day, regime.value, snap.max_position * 100, reason,
        )
        return snap

    # ------------------------------------------------------------ 数据
    def _load_index(self, symbol: str, day: date) -> pd.DataFrame | None:
        """取指数历史用于 Regime 打分。

        性能修复（2026-08-12）：指数历史在回测区间内不可变，按标的一次性拉全量并
        缓存，之后只按 ``day`` 在本地切片（含增量补齐新交易日）。原先每次 detect 用
        滑动起点 ``day - lookback*1.6`` 取数，使 ``get_index_bars`` 的缓存键
        ``(symbol, start, end)`` 每天变化 → 沪深300 / 创业板指每天重复下载。
        """
        try:
            cached = self._idx_cache.get(symbol)
            if cached is None:
                start = (self.fixed_start
                         if getattr(self, "fixed_start", None) is not None
                         else day - pd.Timedelta(days=int(self.lookback * 1.6)))
                df = self.hub.get_index_bars(
                    symbol,
                    start.date() if hasattr(start, "date") else start,
                    day, asof=day,
                )
                if df is None or df.empty:
                    return None
                cached = df.sort_values("date").reset_index(drop=True)
                self._idx_cache[symbol] = cached
            else:
                max_d = pd.to_datetime(cached["date"]).max().date()
                if day > max_d:
                    # 增量补齐新交易日（历史指数不可变，直接追加即可）
                    inc = self.hub.get_index_bars(
                        symbol, max_d + timedelta(days=1), day, asof=day)
                    if inc is not None and not inc.empty:
                        cached = (pd.concat([cached, inc])
                                  .drop_duplicates("date")
                                  .sort_values("date")
                                  .reset_index(drop=True))
                        self._idx_cache[symbol] = cached
            sub = cached[pd.to_datetime(cached["date"]).dt.date <= day]
            return sub if not sub.empty else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("指数 %s 获取失败: %s", symbol, exc)
            return None
