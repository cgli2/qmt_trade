"""回测绩效指标（设计 6.11）。

纯函数，输入权益曲线（日频）与交易记录，输出年化/夏普/回撤/胜率等。
所有计算确定性、无未来函数。

F4 口径纪律（2026-08-12）：短窗口不许年化、小样本不许下胜率结论。
- ``n_days < 120``（约半年）：CAGR/Sharpe 设为 ``None``，``annualized_valid=False`` ——
  47 天算出 44.6% CAGR 是数学正确、经济无意义的数字，不允许再出现在报告里。
- 已平仓笔数 < 20：``win_rate_sample_valid=False`` —— 胜率结论需要 ≥20 笔 round-trip。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..core.trading import Fill

#: 年化指标（CAGR/Sharpe/年化波动）所需的最短交易日数 —— 不足则置 None
MIN_DAYS_FOR_ANNUALIZED = 120
#: win_rate 作为结论所需的最小平仓笔数
MIN_SELLS_FOR_WINRATE = 20


def performance(equity: list[float], *, periods_per_year: int = 252,
                trades: list[Fill] | None = None,
                realized_log: list[float] | None = None) -> dict:
    eq = np.asarray(equity, dtype=float)
    if len(eq) < 2:
        return {"error": "权益序列过短"}
    rets = pd.Series(eq).pct_change().dropna().to_numpy()
    total_ret = eq[-1] / eq[0] - 1.0
    n_days = len(eq) - 1
    years = max(n_days / periods_per_year, 1e-9)

    # ---- 年化指标：窗口过短时置 None（不报无意义数字）----
    annualized_valid = n_days >= MIN_DAYS_FOR_ANNUALIZED
    if annualized_valid and eq[0] > 0:
        cagr = (eq[-1] / eq[0]) ** (1 / years) - 1.0
        vol = float(np.std(rets, ddof=1)) * np.sqrt(periods_per_year) if len(rets) > 1 else 0.0
        sharpe = (cagr - 0.0) / vol if vol > 1e-9 else 0.0  # 无风险利率取 0（A股近似）
    else:
        cagr = None
        vol = None
        sharpe = None

    # 回撤
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    max_dd = float(dd.min())

    # 胜率：基于每笔卖出实现的盈亏；样本不足（<20 笔）只报数字不宣称结论
    win_rate = 0.0
    n_sells = 0
    if realized_log:
        n_sells = len(realized_log)
        wins = sum(1 for p in realized_log if p > 0)
        win_rate = wins / n_sells if n_sells else 0.0

    return {
        "start_equity": float(eq[0]),
        "end_equity": float(eq[-1]),
        "total_return": float(total_ret),
        "cagr": cagr,
        "annual_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "n_days": n_days,
        "annualized_valid": annualized_valid,
        "win_rate": win_rate,
        "win_rate_sample_valid": n_sells >= MIN_SELLS_FOR_WINRATE,
        "n_trades": len(trades or []),
        "n_sells": n_sells,
    }
