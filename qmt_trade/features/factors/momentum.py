"""量价动量类因子（设计 6.2.1 第一类）。

所有因子在长表 panel 上按 symbol 分组向量化计算，输出与 panel 行一一对应。
约定：**值越大越好**。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from ..base import FactorContext, pct_change_n, registry, safe_div

_R = registry.register


def _g(panel: pd.DataFrame, col: str) -> pd.core.groupby.SeriesGroupBy:
    return panel.groupby("symbol", sort=False)[col]


def _rolling_max_drawdown(s: pd.Series, window: int) -> pd.Series:
    """滚动窗口内的**真·最大回撤**（peak-to-trough，返回负值，越接近 0 越好）。

    不能简单写成 ``close / rolling_max - 1`` —— 那算的是"当前距最高点的距离"，
    只反映此刻状态，且与"突破度"因子是同一个单调变换（秩相关恒为 1）。
    真正的最大回撤要看窗口内**每一天**相对其之前峰值的跌幅，取最深的那次。

    实现用 sliding_window_view 展开成 (n-w+1, w) 的二维视图后整体向量化，
    避免 ``rolling.apply`` 的逐窗口 Python 回调（200 票 × 400 天会慢到不可用）。
    """
    arr = s.to_numpy(dtype=float)
    n = arr.size
    out = np.full(n, np.nan)
    if n < window:
        return pd.Series(out, index=s.index)
    win = sliding_window_view(arr, window)          # (n-window+1, window)，只读视图
    peak = np.maximum.accumulate(win, axis=1)       # 窗口内截至每一天的历史峰值
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = np.where(peak > 0, win / peak - 1.0, np.nan)
    out[window - 1:] = np.nanmin(dd, axis=1)        # 最深的一次回撤
    return pd.Series(out, index=s.index)


# ---------------------------------------------------------------- 收益率动量
@_R("ret_20d", "momentum", "20 日收益率", min_periods=21)
def ret_20d(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    return _g(panel, "close").transform(lambda s: pct_change_n(s, 20))


@_R("ret_60d", "momentum", "60 日收益率", min_periods=61)
def ret_60d(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    return _g(panel, "close").transform(lambda s: pct_change_n(s, 60))


@_R("ret_5d_rev", "momentum", "5 日短期反转（取负：短期跌多的反而好）", min_periods=6)
def ret_5d_rev(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    """A 股短周期存在显著反转效应，所以这里取负号。

    注意这个因子和 ret_20d/ret_60d 方向相反是有意为之：
    中期动量 + 短期反转 是 A 股经过长期检验的经典组合。
    """
    return -_g(panel, "close").transform(lambda s: pct_change_n(s, 5))


@_R("mom_12_1", "momentum", "12 个月动量剔除最近 1 个月（经典 12-1 动量）", min_periods=250)
def mom_12_1(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    def f(s: pd.Series) -> pd.Series:
        return safe_div(s.shift(21) - s.shift(243), s.shift(243).abs())

    return _g(panel, "close").transform(f)


# ---------------------------------------------------------------- 均线结构
@_R("ma_align", "momentum", "均线多头排列强度（5>10>20>60 各得一分）", min_periods=61)
def ma_align(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    g = panel.groupby("symbol", sort=False)["close"]
    ma5 = g.transform(lambda s: s.rolling(5, min_periods=5).mean())
    ma10 = g.transform(lambda s: s.rolling(10, min_periods=10).mean())
    ma20 = g.transform(lambda s: s.rolling(20, min_periods=20).mean())
    ma60 = g.transform(lambda s: s.rolling(60, min_periods=60).mean())
    score = (
        (panel["close"] > ma5).astype(float)
        + (ma5 > ma10).astype(float)
        + (ma10 > ma20).astype(float)
        + (ma20 > ma60).astype(float)
    )
    # 任一均线缺失则整体无效，避免用不完整数据打分
    score[ma60.isna()] = np.nan
    return score


@_R("bias_20", "momentum", "20 日乖离率（取负：过度偏离均线是风险不是优势）", min_periods=20)
def bias_20(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    ma20 = panel.groupby("symbol", sort=False)["close"].transform(
        lambda s: s.rolling(20, min_periods=20).mean()
    )
    return -safe_div(panel["close"] - ma20, ma20).abs()


@_R("breakout_60", "momentum", "距 60 日最高价的接近度（越接近突破越强）", min_periods=60)
def breakout_60(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    hh = panel.groupby("symbol", sort=False)["high"].transform(
        lambda s: s.rolling(60, min_periods=60).max()
    )
    return safe_div(panel["close"], hh)


# ---------------------------------------------------------------- 波动与风险
@_R("atr_ratio", "momentum", "ATR20/收盘（取负：同等收益下波动越小越好）", min_periods=21)
def atr_ratio(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    prev_close = panel.groupby("symbol", sort=False)["close"].shift(1)
    tr = pd.concat(
        [
            panel["high"] - panel["low"],
            (panel["high"] - prev_close).abs(),
            (panel["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.groupby(panel["symbol"], sort=False).transform(
        lambda s: s.rolling(20, min_periods=20).mean()
    )
    return -safe_div(atr, panel["close"])


@_R("downside_vol", "momentum", "20 日下行波动率（取负）", min_periods=21)
def downside_vol(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    ret = panel.groupby("symbol", sort=False)["close"].transform(lambda s: pct_change_n(s, 1))
    neg = ret.where(ret < 0, 0.0)
    dv = neg.groupby(panel["symbol"], sort=False).transform(
        lambda s: s.rolling(20, min_periods=20).std()
    )
    return -dv


@_R("max_drawdown_60", "momentum", "60 日窗口内最大回撤（负值，回撤越浅越好）", min_periods=60)
def max_drawdown_60(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    """窗口内 peak-to-trough 的最深回撤。

    值本身是负数（-0.30 = 期间最深跌了 30%），天然满足"越大越好"，无需再取负。
    """
    return _g(panel, "close").transform(lambda s: _rolling_max_drawdown(s, 60))


# ---------------------------------------------------------------- 成交量
@_R("vol_ratio_5_20", "momentum", "量比：5 日均量 / 20 日均量", min_periods=20)
def vol_ratio_5_20(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    g = panel.groupby("symbol", sort=False)["volume"]
    v5 = g.transform(lambda s: s.rolling(5, min_periods=5).mean())
    v20 = g.transform(lambda s: s.rolling(20, min_periods=20).mean())
    return safe_div(v5, v20)


@_R("turnover_stability", "momentum", "换手率稳定性：20 日变异系数（取负）", min_periods=20)
def turnover_stability(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    """换手率的**离散度**而非变化率。

    历史坑：这里原本写的是 ``t5/t20``（换手率 5 日 / 20 日）。
    但 turnover_rate = volume / 流通股本，股本在 20 日窗口内是常数，
    约掉之后 ``t5/t20 ≡ v5/v20``，与 ``vol_ratio_5_20`` 数学上完全等价 ——
    冗余检测里两者秩相关 1.00 就是这么来的。换成变异系数才是新信息：
    换手率忽高忽低说明筹码不稳定、多是游资炒作，取负让稳定的票得高分。
    """
    if "turnover_rate" not in panel.columns:
        return pd.Series(np.nan, index=panel.index)
    g = panel.groupby("symbol", sort=False)["turnover_rate"]
    mean = g.transform(lambda s: s.rolling(20, min_periods=20).mean())
    std = g.transform(lambda s: s.rolling(20, min_periods=20).std())
    return -safe_div(std, mean)


@_R("amount_liquidity", "momentum", "20 日均成交额对数（流动性越好冲击成本越低）", min_periods=20)
def amount_liquidity(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    amt = panel.groupby("symbol", sort=False)["amount"].transform(
        lambda s: s.rolling(20, min_periods=20).mean()
    )
    return np.log1p(amt.clip(lower=0))


@_R("price_volume_corr", "momentum", "20 日价量相关性（放量上涨为佳）", min_periods=21)
def price_volume_corr(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    tmp = pd.DataFrame(
        {
            "symbol": panel["symbol"].to_numpy(),
            "ret": panel.groupby("symbol", sort=False)["close"]
            .transform(lambda s: pct_change_n(s, 1))
            .to_numpy(),
            "dv": panel.groupby("symbol", sort=False)["volume"]
            .transform(lambda s: pct_change_n(s, 1))
            .to_numpy(),
        },
        index=panel.index,
    )
    return tmp.groupby("symbol", sort=False, group_keys=False).apply(
        lambda d: d["ret"].rolling(20, min_periods=20).corr(d["dv"]), include_groups=False
    )


@_R("limit_up_count_20", "momentum", "近 20 日涨停次数（题材热度代理）", min_periods=20)
def limit_up_count_20(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    if "limit_up" not in panel.columns:
        return pd.Series(np.nan, index=panel.index)
    hit = (panel["close"] >= panel["limit_up"] - 1e-6).astype(float)
    return hit.groupby(panel["symbol"], sort=False).transform(
        lambda s: s.rolling(20, min_periods=20).sum()
    )
