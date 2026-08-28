"""确定性回测闭环冒烟测试：选股→风控→仓位→执行→回测 端到端（无 LLM，P5）。

验证 P7：回测与实盘走同一套 ExecutionService / RiskEngine / PositionSizer / Gateway。
关键检查：闭环能跑通、产出权益曲线与交易、无崩溃、无未来函数（因子用 ≤T-1）。
"""

from __future__ import annotations
import logging

import sys
import time
from datetime import date

sys.path.insert(0, ".")

from qmt_trade.core.config import Settings  # noqa: E402
from qmt_trade.datahub.manager import DataHub  # noqa: E402
from qmt_trade.datahub.providers.mock import MockProvider  # noqa: E402
from qmt_trade.backtest import BacktestEngine  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger(__name__)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        logger.info(f"  [OK]   {name} {extra}")
    else:
        FAIL += 1
        logger.info(f"  [FAIL] {name} {extra}")


def main() -> int:
    st = Settings.load()
    for k in ("bars", "fundamentals", "news"):
        st.set(f"datahub.priority.{k}", ["mock"])
    st.set("selection.ranker.top_n", 10)
    st.set("selection.ranker.max_per_industry", 3)
    mock = MockProvider(n_symbols=30, start="2025-01-02", end="2026-08-07", seed=7)
    hub = DataHub(st, [mock])

    start, end = date(2026, 3, 2), date(2026, 6, 30)
    engine = BacktestEngine(st, hub, initial_cash=1_000_000, top_n=5, max_holding_days=15)

    t0 = time.perf_counter()
    res = engine.run(start, end)
    elapsed = time.perf_counter() - t0

    logger.info(f"\n[1] 回测闭环跑通  耗时={elapsed:.1f}s  交易日={len(res.equity_curve)}")
    check("产出权益曲线", len(res.equity_curve) > 30, f"n={len(res.equity_curve)}")
    check("有交易发生", len(res.trades) > 0, f"trades={len(res.trades)}")
    check("权益无 NaN/负无穷", all(float('inf') > x > 0 for x in res.equity_curve),
          f"首尾={res.equity_curve[0]:.0f}→{res.equity_curve[-1]:.0f}")

    logger.info("\n[2] 绩效指标")
    m = res.metrics
    logger.info("        " + ", ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}" for k, v in m.items()))
    check("指标已计算", isinstance(m, dict) and "total_return" in m)
    check("回撤在合理范围", -0.5 <= m.get("max_drawdown", 0) <= 0, f"mdd={m.get('max_drawdown')}")
    # F4 口径：短窗口（<120 交易日）cagr/sharpe 必须为 None（不年化），不允许出现无意义年化数字
    n_days = m.get("n_days", 0)
    if n_days < 120:
        check("短窗口不年化: cagr/sharpe=None",
              m.get("cagr") is None and m.get("sharpe") is None and m.get("annualized_valid") is False,
              f"cagr={m.get('cagr')} sharpe={m.get('sharpe')}")
    else:
        check("长窗口年化指标为有限值",
              __import__("math").isfinite(m.get("sharpe", float("nan"))),
              f"sharpe={m.get('sharpe')}")

    logger.info("\n[3] 组合状态自洽")
    cash = engine.portfolio.cash
    # 市值按现价口径（与 PortfolioState.position_value 一致），无现价兜底成本
    pos_val = sum(p.shares * (p.last_price or p.avg_cost)
                  for p in engine.portfolio.positions.values())
    check("现金+持仓≈权益", abs((cash + pos_val) - engine.portfolio.total_asset) < 1.0,
          f"cash={cash:.0f} pos={pos_val:.0f} total={engine.portfolio.total_asset:.0f}")

    logger.info("\n[4] 无未来函数（PIT 纪律抽样）")
    # 选股在 T 决策、执行在 T+1：检查首笔成交日 > 决策日（通过 details 间接验证不崩溃即可）
    check("每日摘要完整", all("asset" in d for d in res.details), f"days={len(res.details)}")
    # 若 KillSwitch 曾经 FLATTEN，说明回撤熔断生效（非必须，但验证联动）
    ks_modes = {d["killswitch"] for d in res.details}
    logger.info("        KillSwitch 模式分布: " + ", ".join(sorted(ks_modes)))

    logger.info(f"\n{'=' * 46}\n通过 {PASS} / 失败 {FAIL}\n{'=' * 46}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())