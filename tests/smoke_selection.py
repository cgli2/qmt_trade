"""L2-a 选股漏斗冒烟测试：硬过滤 + 排序 + 端到端流水线。

重点验证：
1. 每条硬过滤规则都真的能拦住对应的坏标的，且给出**可读的**淘汰原因
2. 漏斗统计链条自洽（前一级的 after == 后一级的 before）
3. 数据缺失时 fail-safe（宁可漏选不可错选）
4. 行业分散约束生效，且被挤出的高分票有留痕
5. RISK_OFF 直接空池；min_score 门槛生效
6. 结果可复现（P6）：同样输入跑两次必须完全一致

运行：python tests/smoke_selection.py
"""
from __future__ import annotations
import logging

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qmt_trade.core.config import Settings  # noqa: E402
from qmt_trade.datahub.manager import DataHub  # noqa: E402
from qmt_trade.datahub.providers.mock import MockProvider  # noqa: E402
from qmt_trade.features import FeatureEngine, Regime  # noqa: E402
from qmt_trade.selection import (  # noqa: E402

    CandidateSet, Ranker, Screener, SelectionPipeline,
)

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


def build(n=60, start="2024-01-02", end="2026-08-07", seed=42):
    # 用 Settings.load() 而非 get_settings()：后者是 lru_cache 单例，
    # 测试中多次 build 若共用同一对象，前一段的 st.set(...) 会污染后一段（真实 bug 场景）。
    st = Settings.load()
    for k in ("bars", "fundamentals", "news"):
        st.set(f"datahub.priority.{k}", ["mock"])
    mock = MockProvider(n_symbols=n, start=start, end=end, seed=seed)
    return st, mock, DataHub(st, [mock])


def main() -> int:  # noqa: C901 - 冒烟脚本，线性铺开比拆函数更易读
    ASOF = date(2026, 6, 30)
    PREV = date(2026, 6, 29)          # panel 中最后一根可见 bar（盘前 PIT）
    st, mock, hub = build()
    syms = mock.symbols
    eng = FeatureEngine(st, hub)

    # ---------------------------------------------------------------- 造坏样本
    # mock 默认全是"好票"，硬过滤无从验证。这里人工制造各类应被拦下的标的。
    bad = {}
    insts = mock._instruments
    bad["st"] = syms[0]
    insts[syms[0]].is_st = True
    bad["new"] = syms[1]
    insts[syms[1]].list_date = ASOF - timedelta(days=30)      # 上市仅 30 天
    bad["tiny"] = syms[2]
    insts[syms[2]].total_share = 1e6                          # 总股本极小 → 市值不足
    bad["black"] = syms[3]
    bad["suspend"] = syms[4]
    mock.inject(syms[4], PREV, "suspended")
    bad["limit"] = syms[5]
    mock.inject(syms[5], PREV, "limit_up")

    st.set("selection.screener.blacklist", [bad["black"]])
    st.set("selection.screener.min_market_cap", 2_000_000_000)
    st.set("selection.screener.min_amount_20d", 50_000_000)

    panel = eng.build_panel(syms, ASOF)
    instruments = {i.symbol: i for i in hub.get_instruments(syms)}

    logger.info("\n[1] L0 硬过滤：各规则命中")
    scr = Screener(st)
    res = scr.screen(panel, asof=ASOF, instruments=instruments)
    logger.info("        " + res.funnel_report().replace("\n", "\n        "))
    expect = {
        "st": ("ST", bad["st"]),
        "list_days": ("次新股", bad["new"]),
        "market_cap": ("小市值", bad["tiny"]),
        "blacklist": ("黑名单", bad["black"]),
        "suspended": ("停牌", bad["suspend"]),
        "limit_locked": ("一字板", bad["limit"]),
    }
    for rule, (label, sym) in expect.items():
        hit = sym in res.rejected
        check(f"{label}被拦下", hit, f"{sym} → {res.why(sym)}")

    logger.info("\n[2] 淘汰原因可读且指向具体规则")
    check("ST 原因正确", "ST" in res.why(bad["st"]))
    check("次新原因带天数", "上市仅" in res.why(bad["new"]))
    check("市值原因带数值", "亿" in res.why(bad["tiny"]))
    check("未知标的有明确回答", res.why("999999.SH") == "不在候选池内")

    logger.info("\n[3] 漏斗统计自洽")
    chain_ok = all(
        res.stats[i].after == res.stats[i + 1].before for i in range(len(res.stats) - 1)
    )
    check("各级 before/after 链条连续", chain_ok)
    check("首级 before == 全集", res.stats[0].before == len(syms),
          f"{res.stats[0].before} vs {len(syms)}")
    check("末级 after == 通过数", res.stats[-1].after == len(res.passed))
    check("淘汰数守恒", sum(s.removed for s in res.stats) == len(syms) - len(res.passed))
    check("有票通过（未全灭）", 0 < len(res.passed) < len(syms),
          f"{len(res.passed)}/{len(syms)}")

    logger.info("\n[4] fail-safe：数据缺失判不通过")
    dirty = panel.copy()
    victim = res.passed[0]
    dirty.loc[dirty["symbol"] == victim, "amount"] = np.nan
    res_d = Screener(st).screen(dirty, asof=ASOF, instruments=instruments)
    check("成交额缺失的票被剔除", victim not in res_d.passed, f"{victim}")
    check("剔除原因指向成交额", "均额" in res_d.why(victim), res_d.why(victim))
    no_inst = Screener(st).screen(panel, asof=ASOF, instruments={})
    check("无标的信息时不崩且仍能过滤", len(no_inst.passed) > 0, f"n={len(no_inst.passed)}")
    check("无标的信息时市值闸门跳过而非全灭",
          no_inst.stats[-1].after > len(syms) * 0.5, f"n={no_inst.stats[-1].after}")

    logger.info("\n[5] L1 排序与 Top N")
    feats = eng.compute(res.passed, ASOF, regime=Regime.RANGE,
                        panel=panel[panel["symbol"].isin(set(res.passed))])
    scored = feats.frame
    rk = Ranker(st)
    r1 = rk.rank(scored, asof=ASOF, top_n=20, max_per_industry=99)
    check("取满 Top20", r1.n == 20, f"n={r1.n}")
    check("分数单调不增", list(r1.frame["score"]) == sorted(r1.frame["score"], reverse=True))
    check("名次连续从 1 开始", list(r1.frame["rank"]) == list(range(1, r1.n + 1)))
    top_score = scored["score"].nlargest(20).min()
    check("入选者均不低于第20名分数", r1.frame["score"].min() >= top_score - 1e-9)

    logger.info("\n[6] 行业分散约束")
    # 关闭自动放宽，验证严格配额：8 个行业 × 配额 2 = 容量 16 < top_n 20，必然挤出
    rk_strict = Ranker(st)
    rk_strict.relax_if_short = False
    r2 = rk_strict.rank(scored, asof=ASOF, top_n=20, max_per_industry=2)
    dist = r2.frame["industry"].value_counts()
    check("单行业不超过严格配额", dist.max() <= 2, f"max={dist.max()} 分布={dist.to_dict()}")
    check("行业数量足够分散", len(dist) >= 3, f"行业数={len(dist)}")
    check("被挤出的票有记录", len(r2.crowded_out) > 0, f"n={len(r2.crowded_out)}")
    if r2.crowded_out:
        sym0, ind0, sc0, rk0 = r2.crowded_out[0]
        check("挤出记录含行业与原名次", bool(ind0) and rk0 > 0,
              f"{sym0} {ind0} 原#{rk0} score={sc0:.3f}")
    logger.info("        " + r2.report().replace("\n", "\n        "))

    logger.info("\n[7] 行业约束取不满时放宽补位")
    r3 = rk.rank(scored, asof=ASOF, top_n=40, max_per_industry=2)
    check("放宽后仍不超过硬上限", r3.frame["industry"].value_counts().max() <= 3,
          f"max={r3.frame['industry'].value_counts().max()}")
    check("补位数量被记录", r3.relaxed >= 0, f"relaxed={r3.relaxed}")

    logger.info("\n[8] min_score 门槛")
    thr = float(scored["score"].quantile(0.9))
    r4 = rk.rank(scored, asof=ASOF, top_n=100, min_score=thr)
    check("低于门槛全部剔除", (r4.frame["score"] >= thr).all() if r4.n else True)
    check("剔除数量被记录", r4.below_threshold > 0, f"below={r4.below_threshold}")
    r5 = rk.rank(scored, asof=ASOF, top_n=100, min_score=99.0)
    check("门槛过高时返回空池而非兜底放水", r5.n == 0, f"n={r5.n}")

    logger.info("\n[9] 可复现性（P6）")
    a = rk.rank(scored, asof=ASOF, top_n=30, max_per_industry=5)
    b = rk.rank(scored, asof=ASOF, top_n=30, max_per_industry=5)
    check("两次排序结果完全一致", a.selected == b.selected)
    shuffled = scored.sample(frac=1.0, random_state=7).reset_index(drop=True)
    c = rk.rank(shuffled, asof=ASOF, top_n=30, max_per_industry=5)
    check("输入行序打乱后结果不变", a.selected == c.selected)

    logger.info("\n[10] 端到端流水线")
    st.set("selection.ranker.top_n", 25)
    st.set("selection.ranker.max_per_industry", 5)
    pipe = SelectionPipeline(st, hub)
    t0 = time.perf_counter()
    cs = pipe.run(ASOF, universe=syms)
    elapsed = time.perf_counter() - t0
    check("产出 CandidateSet", isinstance(cs, CandidateSet))
    check("候选池非空", cs.n > 0, f"n={cs.n}")
    check("候选数不超过 top_n", cs.n <= 25, f"n={cs.n}")
    check("坏标的一个都没混进来",
          not (set(bad.values()) & set(cs.symbols)),
          f"混入={sorted(set(bad.values()) & set(cs.symbols)) or '无'}")
    check("单行业不超过配额", cs.frame["industry"].value_counts().max() <= 5)
    check("各阶段耗时都有记录",
          {"universe", "regime", "panel", "screen", "factors", "rank"} <= set(cs.timings))
    check("shortlist 可截断", len(cs.shortlist(10)) == min(10, cs.n))
    check("可序列化为 dict", isinstance(cs.to_dict()["screen"], dict))
    check(f"端到端耗时合理 ({elapsed:.1f}s)", elapsed < 60)
    logger.info("        " + cs.report().replace("\n", "\n        "))

    logger.info("\n[11] RISK_OFF 直接空仓")
    st_ro = Settings.load()
    for k in ("bars", "fundamentals", "news"):
        st_ro.set(f"datahub.priority.{k}", ["mock"])
    # 把硬 RISK_OFF 阈值调到极低，任何正常波动都会触发（实际键见 RegimeDetector）
    st_ro.set("regime.vol_extreme", 0.0001)
    st_ro.set("regime.drawdown_riskoff", -0.0001)
    pipe_ro = SelectionPipeline(st_ro, DataHub(st_ro, [mock]))
    cs_ro = pipe_ro.run(ASOF, universe=syms[:20])
    check("RISK_OFF 被识别", cs_ro.regime.regime is Regime.RISK_OFF,
          f"regime={cs_ro.regime.regime.value}")
    check("RISK_OFF 返回空池", cs_ro.is_empty, f"n={cs_ro.n}")
    check("空池带降级说明", any("RISK_OFF" in d for d in cs_ro.degraded), str(cs_ro.degraded))
    check("空池 report 不崩", isinstance(cs_ro.report(), str))

    logger.info("\n[12] 空输入与异常输入")
    empty = Screener(st).screen(pd.DataFrame(), asof=ASOF, instruments={})
    check("空 panel 不崩", empty.n_out == 0)
    check("空打分表不崩", Ranker(st).rank(pd.DataFrame(), asof=ASOF).n == 0)
    check("缺 score 列不崩",
          Ranker(st).rank(pd.DataFrame({"symbol": syms[:3]}), asof=ASOF).n == 0)

    logger.info("\n[13] 全市场规模性能")
    st2, mock2, hub2 = build(n=300, start="2025-01-02", end="2026-08-07")
    st2.set("selection.ranker.top_n", 100)
    st2.set("selection.ranker.max_per_industry", 15)
    pipe2 = SelectionPipeline(st2, hub2)
    t0 = time.perf_counter()
    cs2 = pipe2.run(ASOF, universe=mock2.symbols)
    t_all = time.perf_counter() - t0
    check("300 只票端到端 < 60s", t_all < 60,
          f"{t_all:.1f}s 明细={ {k: round(v, 2) for k, v in cs2.timings.items()} }")
    check("产出 Top100", cs2.n == 100, f"n={cs2.n}")
    check("行业分散生效", cs2.frame["industry"].value_counts().max() <= 15,
          f"max={cs2.frame['industry'].value_counts().max()}")

    logger.info(f"\n{'=' * 46}\n通过 {PASS} / 失败 {FAIL}\n{'=' * 46}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())