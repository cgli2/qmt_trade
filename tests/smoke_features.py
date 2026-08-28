"""L1 特征层冒烟测试：因子库 + 引擎 + Regime + 有效性检验。

重点验证三件事：
1. 因子**没有前视**（用 asof 前后两次计算，历史部分必须完全一致）
2. 打分逻辑正确（缺失值不判死刑、硬负面一票否决、Regime 换权重生效）
3. Regime 四态都能被触发（用构造的极端指数数据）

运行：python tests/smoke_features.py
"""
from __future__ import annotations
import logging

import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qmt_trade.core.config import get_settings  # noqa: E402
from qmt_trade.datahub.manager import DataHub  # noqa: E402
from qmt_trade.datahub.providers.mock import MockProvider  # noqa: E402
from qmt_trade.features import (  # noqa: E402
    FeatureEngine, Regime, RegimeDetector, evaluate_all, registry,
)
from qmt_trade.features.base import FactorContext  # noqa: E402
from qmt_trade.features.validate import correlation_matrix, redundant_pairs  # noqa: E402

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


def build(n=40, start="2024-01-02", end="2026-08-07"):
    st = get_settings()
    st.set("datahub.priority.bars", ["mock"])
    st.set("datahub.priority.fundamentals", ["mock"])
    st.set("datahub.priority.news", ["mock"])
    mock = MockProvider(n_symbols=n, start=start, end=end)
    return st, mock, DataHub(st, [mock])


def main() -> int:
    st, mock, hub = build()
    syms = mock.symbols
    asof = date(2026, 6, 30)

    logger.info("\n[1] 因子注册表")
    names = registry.names()
    check("因子数量 >= 25", len(names) >= 25, f"n={len(names)}")
    cats = {registry.meta(n).category for n in names}
    check("覆盖全部五大类", cats >= {"momentum", "moneyflow", "fundamental", "sentiment", "quality"},
          str(sorted(cats)))
    check("因子名唯一", len(names) == len(set(names)))

    logger.info("\n[2] Panel 构造与 PIT")
    eng = FeatureEngine(st, hub)
    t0 = time.perf_counter()
    panel = eng.build_panel(syms, asof)
    dt = time.perf_counter() - t0
    check("panel 非空", not panel.empty, f"rows={len(panel)} 耗时={dt:.2f}s")
    check("含行业列", "industry" in panel.columns)
    check("盘前切片：不含当日数据",
          panel["date"].max().date() < asof, f"max={panel['date'].max().date()}")

    logger.info("\n[3] 因子计算")
    ctx = FactorContext(asof=datetime(2026, 6, 30, 9, 0), hub=hub, settings=st)
    t0 = time.perf_counter()
    raw = registry.compute_all(panel, ctx)
    dt = time.perf_counter() - t0
    check("产出全部因子列", raw.shape[1] == len(names), f"{raw.shape} 耗时={dt:.2f}s")
    check("行数与 panel 对齐", len(raw) == len(panel))
    allnan = [c for c in raw.columns if raw[c].isna().all()]
    check("无全 NaN 因子", not allnan, f"全NaN={allnan}")
    noninf = all(np.isfinite(raw[c].dropna()).all() for c in raw.columns)
    check("无 inf 值", noninf)

    logger.info("\n[4] 前视检验（关键）")
    # 同一天的因子值，在"数据到 6/30"和"数据到 8/7"两种情况下必须完全一致。
    # 若某因子用了未来数据，后者会因为看得更远而给出不同结果。
    # 注意：两个 panel 必须用**同一个历史起点**，否则 pandas rolling 的增量累加
    # 会产生 1e-16 级浮点差，布尔型因子（如 ma_align）会因此翻转，造成误报。
    fixed_start = date(2025, 1, 2)
    panel = eng.build_panel(syms, asof, start=fixed_start)
    ctx = FactorContext(asof=datetime(2026, 6, 30, 9, 0), hub=hub, settings=st)
    raw = registry.compute_all(panel, ctx)
    eng_far = FeatureEngine(st, hub)
    panel_far = eng_far.build_panel(syms, date(2026, 8, 7), start=fixed_start)
    ctx_far = FactorContext(asof=datetime(2026, 8, 7, 9, 0), hub=hub, settings=st)
    raw_far = registry.compute_all(panel_far, ctx_far)
    key_near = pd.MultiIndex.from_arrays([panel["symbol"], panel["date"]])
    key_far = pd.MultiIndex.from_arrays([panel_far["symbol"], panel_far["date"]])
    a = raw.set_index(key_near)
    b = raw_far.set_index(key_far).reindex(a.index)
    leak = []
    for c in raw.columns:
        if registry.meta(c).needs_extra:
            continue  # 附加数据源按 asof 取数，本身就该不同，另行验证
        x, y = a[c], b[c]
        both = x.notna() & y.notna()
        if both.sum() == 0:
            continue
        if not np.allclose(x[both], y[both], rtol=1e-9, atol=1e-12):
            diff = int((~np.isclose(x[both], y[both], rtol=1e-9, atol=1e-12)).sum())
            leak.append(f"{c}({diff})")
    check("行情类因子无前视", not leak, f"疑似穿越: {leak}")

    logger.info("\n[5] Regime 识别")
    det = RegimeDetector(st, hub)
    snap = det.detect(asof, panel=panel)
    check("返回合法 Regime", isinstance(snap.regime, Regime), snap.regime.value)
    check("仓位上限在 0~1", 0 <= snap.max_position <= 1, f"{snap.max_position:.0%}")
    check("含趋势/波动打分", {"trend", "volatility"} <= set(snap.scores), str(list(snap.scores)))
    check("含宽度指标", "breadth_up_ratio" in snap.metrics)
    check("可序列化", isinstance(snap.to_dict(), dict))

    # 构造极端行情，逼出 RISK_OFF
    class CrashHub:
        def __init__(self, base):
            self.base = base

        def get_index_bars(self, symbol, start=None, end=None, asof=None):
            df = self.base.get_index_bars(symbol, start, end, asof=asof).copy()
            n = len(df)
            # 最后 25 天日跌 1.5% + 高波动
            rng = np.random.default_rng(7)
            shock = np.ones(n)
            shock[-25:] = np.cumprod(1 - 0.015 + rng.normal(0, 0.04, 25))
            df["close"] = df["close"].to_numpy() * shock
            return df

    det_crash = RegimeDetector(st, CrashHub(hub))
    snap_crash = det_crash.detect(asof, panel=panel)
    check("极端下跌触发 RISK_OFF/TREND_DOWN",
          snap_crash.regime in (Regime.RISK_OFF, Regime.TREND_DOWN),
          f"{snap_crash.regime.value} | {snap_crash.reason}")
    check("RISK_OFF 禁止开仓" if snap_crash.regime is Regime.RISK_OFF else "下跌态仓位受限",
          snap_crash.max_position <= 0.2, f"{snap_crash.max_position:.0%}")

    logger.info("\n[6] 打分与排序")
    res = eng.compute(syms, asof, regime=snap, panel=panel)
    check("每票一行", len(res.frame) == panel["symbol"].nunique(), f"n={len(res.frame)}")
    check("score 落在 0~1", res.frame["score"].between(0, 1).all(),
          f"[{res.frame['score'].min():.3f},{res.frame['score'].max():.3f}]")
    check("按 score 降序", res.frame["score"].is_monotonic_decreasing)
    check("无 NaN 分数", res.frame["score"].notna().all())
    check("含分类分列", any(c.startswith("cat_") for c in res.frame.columns),
          str([c for c in res.frame.columns if c.startswith("cat_")]))
    top = res.top(10)
    check("Top10 可取", len(top) == 10 and top["score"].iloc[0] >= top["score"].iloc[-1])

    logger.info("\n[7] Regime 换权重生效")
    r_up = eng.compute(syms, asof, regime=Regime.TREND_UP, panel=panel)
    r_down = eng.compute(syms, asof, regime=Regime.TREND_DOWN, panel=panel)
    check("上涨态动量权重更高",
          r_up.category_weights["momentum"] > r_down.category_weights["momentum"],
          f"{r_up.category_weights['momentum']:.2f} vs {r_down.category_weights['momentum']:.2f}")
    check("下跌态质量权重更高",
          r_down.category_weights["quality"] > r_up.category_weights["quality"])
    rank_up = list(r_up.frame["symbol"])
    rank_down = list(r_down.frame["symbol"])
    check("两种状态排序不同", rank_up[:10] != rank_down[:10])

    logger.info("\n[8] 缺失值不判死刑")
    p2 = panel.copy()
    victim = syms[0]
    # 把某票的成交额抹掉（模拟部分因子缺数据），它不该因此垫底
    p2.loc[p2["symbol"] == victim, "amount"] = np.nan
    r2 = eng.compute(syms, asof, regime=snap, panel=p2)
    v_score = float(r2.frame.loc[r2.frame["symbol"] == victim, "score"].iloc[0])
    check("缺失因子的票未被压到 0", v_score > 0.05, f"score={v_score:.3f}")

    logger.info("\n[9] 硬负面一票否决")
    bad = syms[1]
    mock.inject(bad, "2026-06-20", "investigation")
    hub.cache.clear()
    eng2 = FeatureEngine(st, hub)
    p3 = eng2.build_panel(syms, asof)
    r3 = eng2.compute(syms, asof, regime=snap, panel=p3)
    row = r3.frame[r3.frame["symbol"] == bad]
    if "hard_negative_flag" in r3.frame.columns and not row.empty:
        check("立案调查标的被打 -1 标记", float(row["hard_negative_flag"].iloc[0]) < 0,
              f"flag={row['hard_negative_flag'].iloc[0]}")
        check("其综合分被压到 0", float(row["score"].iloc[0]) == 0.0,
              f"score={row['score'].iloc[0]}")
    else:
        check("硬负面因子存在", False, "未产出 hard_negative_flag 列")

    logger.info("\n[10] 因子有效性检验")
    hist = eng.build_panel(syms[:25], date(2026, 8, 7), history_days=400)
    ctx_h = FactorContext(asof=datetime(2026, 8, 7, 9, 0), hub=hub, settings=st)
    fac = registry.compute_all(hist, ctx_h, registry.names("momentum"))
    merged = pd.concat([hist.reset_index(drop=True), fac.reset_index(drop=True)], axis=1)
    reports = evaluate_all(merged, list(fac.columns), periods=5)
    check("产出全部因子报告", len(reports) == len(fac.columns), f"n={len(reports)}")
    check("IC 计算有效", all(np.isfinite(r.ic_mean) for r in reports))
    empty = [r for r in reports if r.n_periods == 0]
    check("绝大多数因子有有效截面", len(empty) / max(len(reports), 1) <= 0.15,
          f"空报告={[r.name for r in empty] or '无'}")
    # N=0 不一定是 bug（常数因子就该是 0），但必须有可读的诊断，否则运维时无从下手
    check("空报告均带诊断原因", all(r.reject_reason for r in empty),
          f"缺诊断={[r.name for r in empty if not r.reject_reason] or '无'}")
    check("IC 序列长度合理", max(r.n_periods for r in reports) >= 100,
          f"max_periods={max(r.n_periods for r in reports)}")
    for r in reports[:5]:
        logger.info("        " + r.summary())
    corr = correlation_matrix(merged, list(fac.columns))
    check("相关性矩阵对称", corr.shape[0] == corr.shape[1] == len(fac.columns))
    dup = redundant_pairs(merged, list(fac.columns), 0.85)
    logger.info(f"        高相关因子对（>0.85）: {[(a, b, round(c, 2)) for a, b, c in dup[:5]] or '无'}")
    check("冗余检测可运行", isinstance(dup, list))
    # 秩相关 ≥0.98 基本等于同一个公式换了个名字，属于设计缺陷而非数据巧合，必须拦住
    collinear = [(a, b, c) for a, b, c in dup if c >= 0.98]
    check("无完全共线因子对", not collinear,
          f"共线={[(a, b, round(c, 3)) for a, b, c in collinear] or '无'}")

    logger.info("\n[11] 性能")
    st2, mock2, hub2 = build(n=200, start="2025-01-02", end="2026-08-07")
    eng3 = FeatureEngine(st2, hub2)
    t0 = time.perf_counter()
    p200 = eng3.build_panel(mock2.symbols, asof)
    t_panel = time.perf_counter() - t0
    t0 = time.perf_counter()
    r200 = eng3.compute(mock2.symbols, asof, regime=Regime.RANGE, panel=p200)
    t_calc = time.perf_counter() - t0
    check("200 只票全因子 < 30s", t_calc < 30, f"取数{t_panel:.1f}s + 计算{t_calc:.1f}s")
    check("200 只票全部打分", len(r200.frame) == 200, f"n={len(r200.frame)}")

    logger.info(f"\n{'=' * 46}\n通过 {PASS} / 失败 {FAIL}\n{'=' * 46}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())