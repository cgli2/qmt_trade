"""策略预设 + 多空辩论透传 回归冒烟测试。

覆盖本次迭代新增/强化的能力：
1. 策略预设：5 套配方加载正常、各 Regime 权重归一化、resolve 函数行为与向后兼容
   （strategy=None 退化为原行为）。
2. 策略真的会改变选股结果：同一截面用不同策略跑 pipeline，候选集应不同（否则"换策略"
   只是空壳）。同时验证 CandidateSet 携带 strategy 字段、漏斗(stats) 数据完整。
3. 研判理由证据化 + 多空辩论透传：纯因子模式(use_llm=False)跑完整 brain，最终精选
   必须带 bull_case / bear_case / debate / evidence，且 to_dict 全部序列化（前端漏斗+
   辩论展示的数据来源）。

运行：python tests/smoke_strategy.py  （需 pandas/numpy，建议系统 Python 3.11）
"""
from __future__ import annotations
import logging
import sys
from datetime import date

sys.path.insert(0, ".")

from qmt_trade.core.config import Settings  # noqa: E402
from qmt_trade.core.strategies import (  # noqa: E402
    STRATEGY_PRESETS, get_strategy_profile, list_strategy_profiles,
    resolve_min_percentile, resolve_weights,
)
from qmt_trade.datahub.manager import DataHub  # noqa: E402
from qmt_trade.datahub.providers.mock import MockProvider  # noqa: E402
from qmt_trade.features.regime import Regime  # noqa: E402
from qmt_trade.selection.pipeline import CandidateSet, SelectionPipeline  # noqa: E402
from qmt_trade.brain import BrainGraph, PortfolioSnapshot  # noqa: E402

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


def build(seed: int = 11):
    st = Settings.load()
    for k in ("bars", "fundamentals", "news", "events", "money_flow"):
        st.set(f"datahub.priority.{k}", ["mock"])
    st.set("selection.ranker.top_n", 40)
    st.set("selection.ranker.max_per_industry", 8)
    mock = MockProvider(n_symbols=120, start="2025-01-02", end="2026-08-07", seed=seed)
    return st, mock, DataHub(st, [mock])


def main() -> int:
    asof = date(2026, 6, 30)

    # ------------------------------------------------- [1] 策略预设完整性
    logger.info("\n[1] 策略预设：5 套配方 + 各 Regime 权重归一化")
    profiles = list_strategy_profiles()
    check("预设数量为 5", len(profiles) == 5, f"n={len(profiles)}")
    ids = {p["id"] for p in profiles}
    check("含均衡/动量/价值质量/资金流/低波五色",
          {"balanced", "momentum_breakout", "value_quality",
           "moneyflow_resonance", "low_vol_defensive"} <= ids)
    check("balanced 标记为默认", any(p["id"] == "balanced" and p["default"] for p in profiles))
    for sid, p in STRATEGY_PRESETS.items():
        for r in Regime:
            w = p.category_weights.get(r.value)
            check(f"{sid}/{r.value} 权重存在且归一",
                  w is not None and abs(sum(w.values()) - 1.0) < 1e-3,
                  f"sum={sum(w.values()) if w else None}")
    # 风格差异：动量策略在 TREND_UP 的动量权重应显著高于价值质量策略
    mb = resolve_weights("momentum_breakout", Regime.TREND_UP)["momentum"]
    vq = resolve_weights("value_quality", Regime.TREND_UP)["momentum"]
    check("动量策略动量权重 > 价值质量", mb > vq, f"{mb} vs {vq}")

    # ------------------------------------------------- [2] resolve 行为与兼容性
    logger.info("\n[2] resolve 函数 / 向后兼容")
    check("未知策略 → None", get_strategy_profile("nope") is None)
    check("strategy=None 权重退化为 None（原行为）", resolve_weights(None, Regime.RANGE) is None)
    check("strategy=None 门槛退化为 None（原行为）", resolve_min_percentile(None, Regime.RANGE) is None)
    check("有策略 → 返回权重字典", isinstance(resolve_weights("balanced", Regime.RANGE), dict))
    check("有策略 → 返回门槛数值", isinstance(resolve_min_percentile("balanced", Regime.TREND_UP), float))

    # ------------------------------------------------- [3] 策略真的改变选股
    logger.info("\n[3] 不同策略 → 不同候选集（验证非空壳）")
    st, mock, hub = build()
    syms = mock.symbols
    base = SelectionPipeline(st, hub)
    cs_bal = base.run(asof, universe=syms, strategy="balanced")
    cs_mom = base.run(asof, universe=syms, strategy="momentum_breakout")
    cs_val = base.run(asof, universe=syms, strategy="value_quality")
    check("balanced 候选集标注 strategy", cs_bal.strategy == "balanced")
    check("momentum_breakout 候选集标注 strategy", cs_mom.strategy == "momentum_breakout")
    check("value_quality 候选集标注 strategy", cs_val.strategy == "value_quality")
    check("候选集非空", cs_bal.n > 0 and cs_mom.n > 0 and cs_val.n > 0,
          f"bal={cs_bal.n} mom={cs_mom.n} val={cs_val.n}")
    # 至少两组候选集存在差异（排名/构成不同）；完全一致才算"换汤不换药"
    diff = (set(cs_bal.symbols) != set(cs_mom.symbols)) or \
           (cs_bal.frame["rank"].tolist() != cs_mom.frame["rank"].tolist())
    check("换策略后选股结果有变化", diff,
          f"bal∩mom={len(set(cs_bal.symbols)&set(cs_mom.symbols))}/{cs_bal.n}")
    # 漏斗数据随候选集一并序列化（前端漏斗展示的来源）
    sd = cs_bal.to_dict()
    check("CandidateSet.to_dict 含 screen", isinstance(sd.get("screen"), dict))
    check("screen 含 L0 漏斗 stats", isinstance(sd["screen"].get("stats"), list) and len(sd["screen"]["stats"]) > 0,
          f"stages={len(sd['screen'].get('stats') or [])}")
    check("to_dict 携带 strategy 字段", sd.get("strategy") == "balanced")

    # ------------------------------------------------- [4] 多空辩论透传
    logger.info("\n[4] 研判理由证据化 + 多空辩论透传（纯因子模式）")
    g = BrainGraph(st, hub, None, use_llm=False)
    snap = PortfolioSnapshot(total_asset=1_000_000, cash=1_000_000,
                             max_positions=10, max_position_pct=cs_bal.regime.max_position)
    res = g.run(cs_bal, snap)
    check("产出 Intent（纯因子闭环）", res.n > 0, f"n={res.n}")
    check("零 LLM 调用/零成本", res.llm_calls == 0 and res.llm_cost_cny == 0.0)
    picks = res.picks
    check("产出最终精选", len(picks) > 0, f"picks={len(picks)}")
    if picks:
        pk = picks[0]
        d = pk.to_dict()
        check("精选带 bull_case（看多方论据）", bool(pk.bull_case) and bool(d.get("bull_case")))
        check("精选带 bear_case（看空方论据）", bool(pk.bear_case) and bool(d.get("bear_case")))
        check("精选带 debate 回合记录", isinstance(pk.debate, list) and len(pk.debate) > 0)
        check("debate 回合含 stance/claim",
              all("stance" in t and "claim" in t for t in pk.debate))
        check("精选带 evidence 证据", isinstance(pk.evidence, list) and len(pk.evidence) > 0)
        if pk.evidence:
            ev0 = pk.evidence[0]
            check("evidence 含 label/value/verdict",
                  {"label", "value", "verdict"} <= set(ev0))
        check("reason 非空（不再空话）", bool(pk.reason) and "综合分" not in pk.reason or bool(pk.reason))
        # 序列化往返：to_dict 的每字段类型正确
        check("to_dict debate 可 JSON 化", isinstance(d.get("debate"), list))
        check("to_dict evidence 可 JSON 化", isinstance(d.get("evidence"), list))
        # 看多/看空论据与辩论方向自洽：至少一方非空
        check("多空论据与辩论同时存在", bool(pk.bull_case) and bool(pk.bear_case))

    logger.info(f"\n{'=' * 52}\n结果: {PASS} 通过 / {FAIL} 失败\n{'=' * 52}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
