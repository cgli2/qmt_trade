"""资金流因子（设计 6.2.1 第二类）。

资金流数据源不稳定（akshare 接口经常变更、限流），所以这里所有因子都必须
在数据缺失时优雅返回 NaN，而不是抛异常 —— 资金流是加分项不是必需项，
不能因为一个辅助数据源挂了就让整个选股流程停摆（P4 失败安全的具体体现）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import FactorContext, registry, safe_div

_R = registry.register


def _moneyflow_frame(panel: pd.DataFrame, ctx: FactorContext) -> pd.DataFrame | None:
    """取资金流长表。统一列名：``net_inflow``（主力净额）/ ``big_order_ratio``（大单占比）。"""

    def load():
        if ctx.hub is None:
            return None
        syms = panel["symbol"].unique().tolist()
        start = pd.to_datetime(panel["date"]).min()
        end = pd.to_datetime(panel["date"]).max()
        df = ctx.hub.get_money_flow(syms, start.date(), end.date(), asof=ctx.asof)
        if df is None or df.empty:
            return None
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values(["symbol", "date"])

    return ctx.cached("moneyflow_frame", load)  # type: ignore[return-value]


def _rolling_flow(
    panel: pd.DataFrame, ctx: FactorContext, col: str, window: int, how: str = "sum"
) -> pd.Series:
    mf = _moneyflow_frame(panel, ctx)
    if mf is None or col not in mf.columns:
        return pd.Series(np.nan, index=panel.index)
    mf = mf.copy()
    minp = max(2, window // 2)
    mf["_v"] = mf.groupby("symbol", sort=False)[col].transform(
        lambda s: s.rolling(window, min_periods=minp).mean()
        if how == "mean"
        else s.rolling(window, min_periods=minp).sum()
    )
    key = pd.MultiIndex.from_arrays([mf["symbol"], mf["date"]])
    lookup = pd.Series(mf["_v"].to_numpy(), index=key)
    lookup = lookup[~lookup.index.duplicated(keep="last")]
    want = pd.MultiIndex.from_arrays([panel["symbol"], pd.to_datetime(panel["date"])])
    return pd.Series(lookup.reindex(want).to_numpy(), index=panel.index)


@_R("main_net_5d", "moneyflow", "主力资金 5 日净流入", min_periods=5, needs_extra=True)
def main_net_5d(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    return _rolling_flow(panel, ctx, "net_inflow", 5)


@_R("main_net_10d", "moneyflow", "主力资金 10 日净流入", min_periods=10, needs_extra=True)
def main_net_10d(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    return _rolling_flow(panel, ctx, "net_inflow", 10)


@_R("main_net_ratio", "moneyflow", "主力 5 日净流入 / 5 日成交额（消除市值影响）", min_periods=5, needs_extra=True)
def main_net_ratio(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    """绝对净流入会让大盘股永远排前面，必须除以成交额做归一化。"""
    net = _rolling_flow(panel, ctx, "net_inflow", 5)
    amt5 = panel.groupby("symbol", sort=False)["amount"].transform(
        lambda s: s.rolling(5, min_periods=3).sum()
    )
    return safe_div(net, amt5)


@_R("large_order_ratio", "moneyflow", "近 5 日大单占比均值", min_periods=5, needs_extra=True)
def large_order_ratio(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    """大单占比是比例量，滚动应取均值而非求和。"""
    return _rolling_flow(panel, ctx, "big_order_ratio", 5, how="mean")


@_R("flow_consistency", "moneyflow", "资金流方向一致性（近 10 日净流入为正的天数占比）", min_periods=10, needs_extra=True)
def flow_consistency(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    """连续小额净流入比单日巨额流入更可信 —— 后者常是对倒或一次性事件。"""
    mf = _moneyflow_frame(panel, ctx)
    if mf is None or "net_inflow" not in mf.columns:
        return pd.Series(np.nan, index=panel.index)
    mf = mf.copy()
    mf["_pos"] = (mf["net_inflow"] > 0).astype(float)
    mf["_v"] = mf.groupby("symbol", sort=False)["_pos"].transform(
        lambda s: s.rolling(10, min_periods=5).mean()
    )
    key = pd.MultiIndex.from_arrays([mf["symbol"], mf["date"]])
    lookup = pd.Series(mf["_v"].to_numpy(), index=key)
    lookup = lookup[~lookup.index.duplicated(keep="last")]
    want = pd.MultiIndex.from_arrays([panel["symbol"], pd.to_datetime(panel["date"])])
    return pd.Series(lookup.reindex(want).to_numpy(), index=panel.index)
