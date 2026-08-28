#!/usr/bin/env python
"""walk_forward_ic 权重推导核心（不依赖 QMT）的合成自测。

只验证「IC → 权重」的推导规则与 YAML 输出格式，不碰数据源：
- 正 IC  → 权重 = +|IR|
- 负 IC  → 权重 = -|IR|（contra 反向信号）
- 覆盖率 < min_valid_ratio 或 N<=0 或 IC 为 NaN → 权重 = 0（死亡因子）

运行：python scripts/test_walk_forward_ic.py   （仅需 numpy/pandas）
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from walk_forward_ic import derive_weights, weights_yaml  # noqa: E402


class _FakeEngine:
    min_valid_ratio = 0.30


def _build_ic_df() -> pd.DataFrame:
    """合成 IC 表：覆盖正/负/低覆盖/零样本/NaN 五类。"""
    return pd.DataFrame([
        # factor,         IC,    IR,    覆盖率,  N
        ("momentum",      0.05,  0.42,  0.85,  200),   # 正 IC → +0.42
        ("reversal",     -0.04, -0.31,  0.80,  190),   # 负 IC → -0.31
        ("noise_a",       0.01,  0.05,  0.12,  150),   # 覆盖不足 → 0
        ("dead_b",        0.03,  0.20,  0.90,    0),    # N=0 → 0
        ("nan_c",         np.nan, 0.10, 0.90,  100),   # IC NaN → 0
    ], columns=["factor", "IC", "IR", "覆盖率", "N"])


def main() -> int:
    engine = _FakeEngine()
    ic_df = _build_ic_df()
    w = derive_weights(ic_df, engine)

    checks = [
        ("正 IC → +|IR|",  w["momentum"],            0.42),
        ("负 IC → -|IR|",  w["reversal"],           -0.31),
        ("覆盖不足 → 0",   w["noise_a"],              0.0),
        ("N=0 → 0",        w["dead_b"],               0.0),
        ("IC NaN → 0",     w["nan_c"],                0.0),
    ]
    ok = True
    for name, got, exp in checks:
        passed = abs(got - exp) < 1e-9
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name:<18} got={got:+.3f} exp={exp:+.3f}")

    # YAML 输出格式校验
    yaml_block = weights_yaml(w, train="2024~2025", test="2026", periods=5)
    fmt_ok = ("factor_weights:" in yaml_block
              and "momentum: 0.420" in yaml_block
              and "reversal: -0.310" in yaml_block)
    ok &= fmt_ok
    print(f"  [{'PASS' if fmt_ok else 'FAIL'}] YAML 含正/反向权重且格式正确")

    print("\n合成自测:", "ALL PASS ✅" if ok else "FAILED ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
