"""基本面因子（设计 6.2.1 第三类）。

**PIT 关键点**：财务数据一律通过 ``DataHub.get_latest_fundamentals(asof=...)`` 取，
它按 ``ann_date``（公告日）过滤而非报告期。用报告期对齐是 qmt_etf 回测的典型穿越 ——
2026Q1 报告在 4 月底才公告，但按报告期对齐会让 4 月 1 日就能看到，凭空多出一个月的先知。

由于财务数据是"每票一个快照"而非时序，这里的实现是：
取 asof 时点各票的最新一期财务，广播到该票 panel 的所有行。
回测中引擎会按天滚动调用，因此每天拿到的都是当天可见的那一期。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import FactorContext, registry, safe_div

_R = registry.register


def _fundamental_frame(panel: pd.DataFrame, ctx: FactorContext) -> pd.DataFrame | None:
    """把 asof 可见的最新财务快照整理成 DataFrame（index=symbol）。"""

    def load():
        if ctx.hub is None:
            return None
        syms = panel["symbol"].unique().tolist()
        recs = ctx.hub.get_latest_fundamentals(syms, asof=ctx.asof)
        if not recs:
            return None
        rows = []
        for sym, f in recs.items():
            rows.append(
                {
                    "symbol": sym,
                    "ann_date": getattr(f, "ann_date", None),
                    "report_period": getattr(f, "report_period", None),
                    "roe": getattr(f, "roe", np.nan),
                    "revenue": getattr(f, "revenue", np.nan),
                    "net_profit": getattr(f, "net_profit", np.nan),
                    "revenue_yoy": getattr(f, "revenue_yoy", np.nan),
                    "profit_yoy": getattr(f, "profit_yoy", np.nan),
                    "gross_margin": getattr(f, "gross_margin", np.nan),
                    "debt_ratio": getattr(f, "debt_ratio", np.nan),
                    "ocf": getattr(f, "ocf", np.nan),
                    "eps": getattr(f, "eps", np.nan),
                    "bps": getattr(f, "bps", np.nan),
                }
            )
        return pd.DataFrame(rows).set_index("symbol")

    return ctx.cached("fundamentals_frame", load)  # type: ignore[return-value]


def _broadcast(panel: pd.DataFrame, ctx: FactorContext, col: str) -> pd.Series:
    fdf = _fundamental_frame(panel, ctx)
    if fdf is None or col not in fdf.columns:
        return pd.Series(np.nan, index=panel.index)
    return panel["symbol"].map(fdf[col]).astype(float)


# ---------------------------------------------------------------- 盈利能力
@_R("roe", "fundamental", "净资产收益率", min_periods=1, needs_extra=True)
def roe(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    return _broadcast(panel, ctx, "roe")


@_R("gross_margin", "fundamental", "毛利率", min_periods=1, needs_extra=True)
def gross_margin(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    return _broadcast(panel, ctx, "gross_margin")


@_R("profit_yoy", "fundamental", "净利润同比增速", min_periods=1, needs_extra=True)
def profit_yoy(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    return _broadcast(panel, ctx, "profit_yoy")


@_R("revenue_yoy", "fundamental", "营收同比增速", min_periods=1, needs_extra=True)
def revenue_yoy(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    return _broadcast(panel, ctx, "revenue_yoy")


# ---------------------------------------------------------------- 估值
#
# 不直接取数据源的 PE/PB 字段，而是用 ``当日收盘价 / 每股指标`` 现算。两个理由：
#   1. PIT 更干净：EPS/BPS 来自已公告财报（按 ann_date 过滤），close 来自已切片行情，
#      两者都可追溯；而数据源给的 PE 往往是"最新值"快照，回测里一用就穿越。
#   2. 复权一致：panel 用后复权价，估值随之一致，不会出现除权日估值跳变。
# 统一用倒数形式（盈利收益率 / 账面市值比），保证"越大越好"。


@_R("earnings_yield", "fundamental", "盈利收益率 EPS/Price（PE 倒数，越大越便宜）", min_periods=1, needs_extra=True)
def earnings_yield(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    eps = _broadcast(panel, ctx, "eps")
    # 亏损股（EPS<0）没有估值意义，置 NaN；否则它会因"PE 为负"被排到最便宜那一档
    eps = eps.where(eps > 0)
    return safe_div(eps, panel["close"])


@_R("book_to_price", "fundamental", "账面市值比 BPS/Price（PB 倒数）", min_periods=1, needs_extra=True)
def book_to_price(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    bps = _broadcast(panel, ctx, "bps")
    bps = bps.where(bps > 0)
    return safe_div(bps, panel["close"])


# ---------------------------------------------------------------- 质量
@_R("ocf_quality", "quality", "经营现金流 / 净利润（现金含量，防财务造假）", min_periods=1, needs_extra=True)
def ocf_quality(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    """净利润高但经营现金流长期为负，是最经典的造假信号之一。

    这个因子在 TradingAgents-CN 里完全没有 —— 它的基本面分析只看财报数字本身，
    不做勾稽验证。
    """
    ocf = _broadcast(panel, ctx, "ocf")
    npf = _broadcast(panel, ctx, "net_profit")
    ratio = safe_div(ocf, npf.abs())
    return ratio.clip(-3, 3)


@_R("debt_safety", "quality", "资产负债率安全度（取负：负债越低越好）", min_periods=1, needs_extra=True)
def debt_safety(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    return -_broadcast(panel, ctx, "debt_ratio")


@_R("size_inv", "quality", "市值倒数对数（小市值溢价，壳股已在硬过滤排除）", min_periods=1, needs_extra=True)
def size_inv(panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
    """市值 = 收盘价 × 总股本，总股本取自标的静态信息。

    小市值溢价在 A 股长期显著，但必须配合硬过滤（市值 < 20 亿剔除）使用，
    否则会选出一堆壳股和退市风险股 —— 那不是 alpha，是在捡地雷。
    """

    def load():
        if ctx.hub is None:
            return None
        try:
            infos = ctx.hub.get_instruments(panel["symbol"].unique().tolist())
        except Exception:  # noqa: BLE001
            return None
        return {i.symbol: float(getattr(i, "total_share", 0) or 0) for i in infos}

    shares = ctx.cached("total_share_map", load)
    if not shares:
        return pd.Series(np.nan, index=panel.index)
    ts = panel["symbol"].map(shares).astype(float)
    mv = (panel["close"] * ts).where(lambda x: x > 0)
    return -np.log(mv)
