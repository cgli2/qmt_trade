"""情绪与题材因子（设计 6.2.1 第四类）。

**这里刻意不调用 LLM**。原因见设计 P5：因子层要能脱离 LLM 独立跑通回测。
新闻情感分用的是数据源自带的规则化打分（akshare/东财的情绪标签），
LLM 的深度解读放在 L2-b，只对已经进入 Top100 的票做，成本可控。

TradingAgents-CN 的做法是每只票都喂给 LLM 做情感分析，5000 只票根本跑不起，
它能跑是因为它只分析用户手动指定的单只票 —— 那不是选股系统。
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from ...datahub.types import HARD_NEGATIVE_EVENTS
from ..base import FactorContext, registry, safe_div

_R = registry.register


def _news_frame(panel: pd.DataFrame, ctx: FactorContext) -> pd.DataFrame | None:
    def load():
        if ctx.hub is None:
            return None
        syms = panel["symbol"].unique().tolist()
        start = pd.to_datetime(panel["date"]).min() - pd.Timedelta(days=5)
        items = ctx.hub.get_news(syms, start.to_pydatetime(), limit=20000, asof=ctx.asof)
        if not items:
            return None
        return pd.DataFrame(
            [
                {
                    "symbol": n.symbol,
                    "time": pd.Timestamp(n.publish_time),
                    "sentiment": float(getattr(n, "sentiment", 0.0) or 0.0),
                    "importance": float(getattr(n, "importance", 0.0) or 0.0),
                }
                for n in items
                if n.symbol
            ]
        )

    return ctx.cached("news_frame", load)  # type: ignore[return-value]


def _events_frame(panel: pd.DataFrame, ctx: FactorContext) -> pd.DataFrame | None:
    def load():
        if ctx.hub is None:
            return None
        syms = panel["symbol"].unique().tolist()
        start = pd.to_datetime(panel["date"]).min() - pd.Timedelta(days=30)
        items = ctx.hub.get_events(syms, start.to_pydatetime(), asof=ctx.asof)
        if not items:
            return None
        return pd.DataFrame(
            [
                {
                    "symbol": e.symbol,
                    "time": pd.Timestamp(e.ann_time),
                    "category": e.category,
                    "sentiment": float(getattr(e, "sentiment", 0.0) or 0.0),
                    "importance": float(getattr(e, "importance", 0.0) or 0.0),
                    "hard_negative": e.category in HARD_NEGATIVE_EVENTS,
                }
                for e in items
                if e.symbol
            ]
        )

    return ctx.cached("events_frame", load)  # type: ignore[return-value]


def _window_agg(
    panel: pd.DataFrame,
    src: pd.DataFrame | None,
    days: int,
    value_col: str,
    how: str = "mean",
) -> pd.Series:
    """对每个 (symbol, date) 聚合过去 ``days`` 个自然日内的记录。

    实现方式：先按 (symbol, 自然日) 汇总成 sum/count 两张宽表，
    在**自然日网格**上做一次 rolling，再按 (day, symbol) 反查回 panel。

    最初写成逐行筛选（``for row in panel: src[mask]``），40 只票就要 20 秒 ——
    12840 行 × 每行一次 DataFrame 布尔索引，纯属自找麻烦。
    改成向量化后同样规模 < 0.1 秒。教训：**任何按行遍历 panel 的因子实现都是错的**。
    """
    if src is None or src.empty:
        return pd.Series(np.nan, index=panel.index)
    s = src.copy()
    s["day"] = pd.to_datetime(s["time"]).dt.normalize()
    agg = s.groupby(["symbol", "day"]).agg(_s=(value_col, "sum"), _n=(value_col, "size")).reset_index()

    panel_days = pd.to_datetime(panel["date"]).dt.normalize()
    lo = min(agg["day"].min(), panel_days.min()) - pd.Timedelta(days=days + 1)
    hi = max(agg["day"].max(), panel_days.max())
    grid = pd.date_range(lo, hi, freq="D")

    piv_s = agg.pivot(index="day", columns="symbol", values="_s").reindex(grid).fillna(0.0)
    piv_n = agg.pivot(index="day", columns="symbol", values="_n").reindex(grid).fillna(0.0)
    roll_s = piv_s.rolling(days, min_periods=1).sum()
    roll_n = piv_n.rolling(days, min_periods=1).sum()

    if how == "sum":
        res = roll_s
    elif how == "count":
        res = roll_n
    else:  # mean：按条数加权，窗口内无记录时为 NaN 而非 0（没有新闻 ≠ 情感中性为 0）
        res = roll_s / roll_n.replace(0, np.nan)

    stacked = res.stack(future_stack=True)  # index = (day, symbol)
    want = pd.MultiIndex.from_arrays([panel_days, panel["symbol"]])
    return pd.Series(stacked.reindex(want).to_numpy(), index=panel.index)


@_R("news_sentiment_5d", "sentiment", "近 5 日新闻情感均值", min_periods=1, needs_extra=True)
def news_sentiment_5d(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    return _window_agg(panel, _news_frame(panel, ctx), 5, "sentiment", "mean")


@_R("news_heat_5d", "sentiment", "近 5 日新闻条数（关注度）", min_periods=1, needs_extra=True)
def news_heat_5d(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    return _window_agg(panel, _news_frame(panel, ctx), 5, "sentiment", "count")


@_R("event_sentiment_20d", "sentiment", "近 20 日公告情感（按重要性加权）", min_periods=1, needs_extra=True)
def event_sentiment_20d(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    ev = _events_frame(panel, ctx)
    if ev is None or ev.empty:
        return pd.Series(np.nan, index=panel.index)
    ev = ev.copy()
    ev["weighted"] = ev["sentiment"] * (0.3 + 0.7 * ev["importance"])
    return _window_agg(panel, ev, 20, "weighted", "mean")


@_R("hard_negative_flag", "sentiment", "近 60 日硬负面事件标记（-1 表示中招，rule-first 强制规避）", min_periods=1, needs_extra=True)
def hard_negative_flag(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    """立案调查/监管处罚这类事件**不交给 LLM 判断**，规则直接一票否决。

    设计 6.5.1 明确：硬负面事件走 rule-first 路径。LLM 可能被"利空出尽"之类的
    叙事说服，但监管风险的尾部损失不对称，不值得赌。
    """
    ev = _events_frame(panel, ctx)
    if ev is None or ev.empty:
        return pd.Series(0.0, index=panel.index)
    hard = ev[ev["hard_negative"]].copy()
    if hard.empty:
        return pd.Series(0.0, index=panel.index)
    hard["flag"] = 1.0
    hit = _window_agg(panel, hard, 60, "flag", "sum")
    return -(hit.fillna(0.0) > 0).astype(float)


@_R("industry_momentum", "sentiment", "所属行业 20 日平均涨幅（题材热度）", min_periods=21)
def industry_momentum(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    """行业动量是 A 股最强的 beta 之一：选对板块比选对个股重要。"""
    if "industry" not in panel.columns:
        return pd.Series(np.nan, index=panel.index)
    ret20 = panel.groupby("symbol", sort=False)["close"].transform(
        lambda s: safe_div(s - s.shift(20), s.shift(20).abs())
    )
    tmp = pd.DataFrame(
        {"industry": panel["industry"].to_numpy(), "date": panel["date"].to_numpy(), "r": ret20.to_numpy()},
        index=panel.index,
    )
    return tmp.groupby(["date", "industry"], sort=False)["r"].transform("mean")
