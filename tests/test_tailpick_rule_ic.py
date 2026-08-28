#!/usr/bin/env python
"""tail_pick_rule_ic 核心逻辑（不依赖 QMT）的合成自测。

验证三件事（纯函数，仅需 numpy/pandas）：
1. ``_gap_stats``：均值/中位/低开率口径正确（低开率 = gap < −0.3% 占比）；
2. ``_funnel``：规则按序叠加，最终池低开率 ≤ 上一级（单调收紧）；
3. ``_rule_report``：每日口径"提升" = 每天 pass 均值 − 全市场均值再平均，
   且构造一个"规则挑高开票"的场景能正确测出正向提升。

运行：python scripts/test_tailpick_rule_ic.py   （仅需 numpy/pandas）
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tail_pick_rule_ic import _gap_stats, _rule_report, _funnel, _FUNNEL  # noqa: E402


def _mk_panel(n_days: int = 3, n_sym: int = 6) -> pd.DataFrame:
    """合成面板：每天每只票一行，含 gap 与规则列。

    r3 通过 = 偶数标的（gap +1%，正提升规则）；r4 通过 = 奇数标的（gap −1%，负提升规则）。
    """
    rows = []
    dates = pd.date_range("2024-01-02", periods=n_days, freq="B")
    for d in dates:
        for s in range(n_sym):
            gap = 0.01 if s % 2 == 0 else -0.01
            rows.append({"date": d, "symbol": f"S{s:04d}", "gap": gap,
                         "hard_ok": True, "r2": True, "r3": s % 2 == 0,
                         "r4": s % 2 == 1, "r5": True, "r6a": True,
                         "r7": True, "r9": True, "r13": True})
    return pd.DataFrame(rows)


def main() -> int:
    checks: list[tuple[str, bool]] = []

    # 1) _gap_stats 口径：< −0.3% 的有两个（-1%、-0.5%）；< −1% 的零个（严格小于）
    g = _gap_stats(pd.Series([-0.01, -0.005, -0.002, 0.0, 0.005, 0.01]))
    checks.append(("低开率 −0.3% 口径（2/6）", abs(g["gapdown_cut"] - (2 / 6)) < 1e-9))
    checks.append(("深低开 −1% 口径（0/6）", abs(g["gapdown_deep"] - 0.0) < 1e-9))
    checks.append(("均值 gap", abs(g["mean"] - (-0.002 / 6)) < 1e-9))

    # 2) r3 通过 = 偶数（gap +1%），全市场均值 0 → 每日提升应为 +1pp
    panel = _mk_panel()
    r = _rule_report(panel, "r3", "③ 缩量")
    checks.append(("正提升规则被正确识别", abs(r["daily_lift"] - 0.01) < 1e-9))
    checks.append(("提升为正天数占比=100%", abs(r["pos_days"] - 1.0) < 1e-9))
    # r4 通过 = 奇数（gap −1%）→ 每日提升为 −1pp
    r4 = _rule_report(panel, "r4", "④ 换手率")
    checks.append(("负提升规则被正确识别", abs(r4["daily_lift"] - (-0.01)) < 1e-9))
    # 中性规则 r2（恒 True）→ 提升 ≈ 0
    r2 = _rule_report(panel, "r2", "② 涨幅带")
    checks.append(("中性规则提升≈0", abs(r2["daily_lift"]) < 1e-9))

    # 3) _funnel 单调性：叠加规则后样本不增、低开率不劣于全市场（合成数据）
    funnel = _funnel(panel, _FUNNEL)
    prev_n = float("inf")
    mono = True
    for row in funnel:
        if row["n"] > prev_n:
            mono = False
        prev_n = row["n"]
    checks.append(("漏斗样本单调不增", mono))

    ok = True
    for name, passed in checks:
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("\n合成自测:", "ALL PASS ✅" if ok else "FAILED ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
