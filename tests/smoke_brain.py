"""L2-b LLM 决策层冒烟测试。

验证两条路径都能跑通（这是 P5 的核心诉求）：
  A. 纯因子模式（use_llm=False）—— 无任何外部依赖，确定性；
  B. LLM 增强模式（MockLLM）—— 走完整 4 分析师并行 + 辩论 + 三级审批。

并验证安全阀：事实校验器抓幻觉、硬负面事件短路、组合约束否决、
LLM 不能放宽风险、成本熔断自动降级、结果可复现。
"""

from __future__ import annotations
import logging

import sys
import time
from datetime import date, timedelta

sys.path.insert(0, ".")

from qmt_trade.core.config import Settings  # noqa: E402
from qmt_trade.core.errors import LLMBudgetExceeded  # noqa: E402
from qmt_trade.datahub.manager import DataHub  # noqa: E402
from qmt_trade.datahub.providers.mock import MockProvider  # noqa: E402
from qmt_trade.selection.pipeline import SelectionPipeline  # noqa: E402
from qmt_trade.brain import (  # noqa: E402
    BrainGraph, FactPackBuilder, PortfolioSnapshot, check_intent, verify_numbers,
)
from qmt_trade.brain.agents import DebateModerator, RiskOfficer, build_analysts  # noqa: E402
from qmt_trade.brain.agents.base import extract_numbers, parse_json_loose  # noqa: E402
from qmt_trade.brain.llm import CostTracker, LLMClient, LLMResponse, MockLLM  # noqa: E402
from qmt_trade.brain.state import AgentState, FactPack  # noqa: E402

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


def build(seed: int = 7):
    st = Settings.load()
    for k in ("bars", "fundamentals", "news", "events", "money_flow"):
        st.set(f"datahub.priority.{k}", ["mock"])
    st.set("selection.ranker.top_n", 15)
    st.set("selection.ranker.max_per_industry", 3)
    hub = DataHub(st, [MockProvider(n_symbols=40, start="2025-01-02",
                                    end="2026-08-07", seed=seed)])
    return st, hub


def main() -> int:
    asof = date(2026, 6, 30)
    st, hub = build()
    cs = SelectionPipeline(st, hub).run(asof)
    snap = PortfolioSnapshot(total_asset=1_000_000, cash=1_000_000,
                             max_positions=10, max_position_pct=cs.regime.max_position)

    # ---------------------------------------------------------------- 1 FactPack
    logger.info(f"\n[1] FactPack Builder（非 LLM，防幻觉第一道闸）  候选={cs.n}")
    row = cs.frame.iloc[0]
    fp = FactPackBuilder(hub).build(row["symbol"], row, asof)
    check("事实条数充足", len(fp.facts) >= 15, f"n={len(fp.facts)}")
    check("数值索引已建", len(fp.numerics) >= 10, f"n={len(fp.numerics)}")
    check("显式标注数据日期", all(f.asof is not None for f in fp.facts))
    check("缺失字段显式列出", isinstance(fp.missing, list),
          f"missing={len(fp.missing)}")
    check("覆盖率可计算", 0.0 < fp.coverage <= 1.0, f"{fp.coverage:.0%}")
    rendered = fp.render()
    check("渲染文本含基准日", str(asof) in rendered or str(row["date"])[:10] in rendered)

    # ------------------------------------------------------- 2 纯因子模式（P5）
    logger.info("\n[2] 纯因子模式 use_llm=False（P5：无 LLM 完整闭环）")
    g0 = BrainGraph(st, hub, None, use_llm=False)
    t0 = time.perf_counter()
    r0 = g0.run(cs, snap)
    e0 = time.perf_counter() - t0
    check("产出 Intent", r0.n > 0, f"n={r0.n} 否决={len(r0.rejected)} 耗时={e0:.2f}s")
    check("零 LLM 调用", r0.llm_calls == 0)
    check("零成本", r0.llm_cost_cny == 0.0)
    check("Intent 数不超上限", r0.n <= g0.max_intents, f"{r0.n}<={g0.max_intents}")
    if r0.n:
        it = r0.intents[0]
        check("Intent 含止损", it.stop_loss_value > 0, f"{it.stop_loss_value:.1%}")
        check("Intent 含失效条件", len(it.invalidation_checks) > 0)
        check("Intent 含证据链", len(it.evidence) >= 2)
        check("Intent 含 agent 投票", len(it.agent_votes) >= 4,
              f"votes={len(it.agent_votes)}")
        check("Intent 通过 factcheck", check_intent(it, asof).ok)
        check("valid_until 在未来", it.valid_until >= asof)

    # ---------------------------------------------------- 3 可复现（P6）
    logger.info("\n[3] 确定性可复现（P6）")
    st2, hub2 = build()
    cs2 = SelectionPipeline(st2, hub2).run(asof)
    r0b = BrainGraph(st2, hub2, None, use_llm=False).run(cs2, snap)
    same = ([i.symbol for i in r0.intents] == [i.symbol for i in r0b.intents]
            and [i.fingerprint() for i in r0.intents] == [i.fingerprint() for i in r0b.intents])
    check("两次运行结果完全一致", same,
          f"{[i.symbol for i in r0.intents][:3]} vs {[i.symbol for i in r0b.intents][:3]}")

    # ------------------------------------------------- 4 LLM 增强（MockLLM）
    logger.info("\n[4] LLM 增强模式（MockLLM，4 分析师并行 + 辩论）")
    client = LLMClient(MockLLM(latency_ms=0), tracker=CostTracker())
    g1 = BrainGraph(st, hub, client, use_llm=True, max_workers=4)
    t1 = time.perf_counter()
    r1 = g1.run(cs, snap)
    e1 = time.perf_counter() - t1
    check("LLM 模式产出 Intent", r1.n > 0, f"n={r1.n} 耗时={e1:.2f}s")
    check("确实调用了 LLM", r1.llm_calls > 0, f"calls={r1.llm_calls}")
    sym0 = next(iter(r1.states))
    s0 = r1.states[sym0]
    check("四位分析师都出报告", len({"technical", "fundamental", "moneyflow",
                                     "sentiment"} & set(s0.reports)) == 4,
          f"reports={sorted(s0.reports)}")
    check("辩论有结构化记录", len(s0.debate) >= 2, f"turns={len(s0.debate)}")
    check("辩论轮次受控 ≤3", max((t.round_no for t in s0.debate), default=0) <= 3)
    check("节点耗时已统计", "analysts" in s0.node_timings and "debate" in s0.node_timings)
    check("生成了投资论点", len(s0.thesis) > 10, f"len={len(s0.thesis)}")

    # ------------------------------------------------------ 5 事实校验器
    logger.info("\n[5] 事实校验器（幻觉检测）")
    fake = FactPack(symbol="T", asof=asof)
    fake.add("PE", 23.4)
    fake.add("ROE", 0.15)
    check("引用真实数字 → 通过", verify_numbers([23.4, 0.15], fake).ok)
    check("引用衍生量(百分比) → 通过", verify_numbers([15.0], fake).ok)
    bad = verify_numbers([999.7, 888.3, 777.1], fake)
    check("整段编造 → 判幻觉", not bad.ok, bad.issues[:1])
    check("年份等良性数字不误报", verify_numbers([2026.0, 100.0, 23.4], fake).ok)
    check("空引用不报错", verify_numbers([], fake).ok)

    # -------------------------------------------------- 6 硬负面事件短路
    logger.info("\n[6] 规则先行：硬负面事件直接否决（不等 LLM）")
    hn_row = dict(cs.frame.iloc[0])
    hn_row["hard_negative_flag"] = 1.0
    st_hn = g0.run_one(hn_row["symbol"], hn_row, asof, snap, regime="TREND_UP")
    check("硬负面 → veto", bool(st_hn.veto_reason), st_hn.veto_reason)
    check("硬负面 → 打 flag", "HARD_NEGATIVE" in st_hn.risk_flags)
    check("硬负面 → 不产出 Intent",
          g0.risk_officer.make_intent(st_hn) is None)
    check("硬负面 → 短路未进辩论", len(st_hn.debate) == 0)

    # ----------------------------------------------------- 7 组合约束否决
    logger.info("\n[7] 组合经理：组合层面约束")
    full = PortfolioSnapshot(total_asset=1_000_000, cash=500_000,
                             position_weight={f"X{i}.SZ": 0.05 for i in range(10)},
                             max_positions=10, max_position_pct=0.8)
    st_full = g0.run_one(row["symbol"], row, asof, full, regime="TREND_UP")
    check("持仓数已满 → veto", "持仓" in st_full.veto_reason or "仓位" in st_full.veto_reason,
          st_full.veto_reason)

    ind = str(row.get("industry") or "未知")
    conc = PortfolioSnapshot(total_asset=1_000_000, cash=800_000,
                             position_weight={"Y1.SZ": 0.35},
                             industry_weight={ind: 0.35},
                             max_positions=10, max_position_pct=0.8)
    st_conc = g0.run_one(row["symbol"], row, asof, conc, regime="TREND_UP")
    pm = st_conc.reports.get("portfolio_manager")
    check("行业集中度 → 降分或标记", pm is not None and (
        pm.score <= 0.40 or "INDUSTRY_CONCENTRATION" in st_conc.risk_flags),
        f"score={pm.score if pm else 'NA'}")

    cashless = PortfolioSnapshot(total_asset=1_000_000, cash=10_000,
                                 position_weight={"Z1.SZ": 0.2},
                                 max_positions=10, max_position_pct=0.8)
    st_cash = g0.run_one(row["symbol"], row, asof, cashless, regime="TREND_UP")
    pm2 = st_cash.reports.get("portfolio_manager")
    check("现金不足 → 降分", pm2 is not None and pm2.score <= 0.36,
          f"score={pm2.score if pm2 else 'NA'}")

    # --------------------------------------------- 8 LLM 不能放宽风险（P1）
    logger.info("\n[8] P1 底线：LLM 只能收紧风险，不能放宽")
    ro = RiskOfficer(None, use_llm=False)
    s_probe = g0.run_one(row["symbol"], row, asof, snap, regime="TREND_UP")
    stop_before = float(s_probe.votes["risk_stop_pct"])
    rep = s_probe.reports["risk_officer"]
    # 模拟 LLM 想把止损放宽到 30%（负的 tighten）
    ro._merge_llm(rep, {"stop_tighten_pct": -0.25}, "", s_probe)
    check("LLM 放宽止损被忽略",
          float(s_probe.votes["risk_stop_pct"]) == stop_before,
          f"{stop_before:.4f}")
    ro._merge_llm(rep, {"stop_tighten_pct": 0.02}, "", s_probe)
    check("LLM 收紧止损被采纳",
          float(s_probe.votes["risk_stop_pct"]) < stop_before,
          f"{stop_before:.4f} → {float(s_probe.votes['risk_stop_pct']):.4f}")

    intent = ro.make_intent(s_probe)
    check("max_weight_hint 受 schema 硬约束",
          intent is None or intent.max_weight_hint <= 0.30)
    check("RISK_OFF 环境压缩预算",
          (lambda a, b: b < a)(
              (lambda s: ro.make_intent(s).risk_budget_hint if ro.make_intent(s) else 1)(
                  g0.run_one(row["symbol"], row, asof, snap, regime="TREND_UP")),
              (lambda s: ro.make_intent(s).risk_budget_hint if ro.make_intent(s) else 0)(
                  g0.run_one(row["symbol"], row, asof, snap, regime="RISK_OFF"))))

    # ------------------------------------------------------- 9 成本熔断
    logger.info("\n[9] 成本硬熔断 → 自动降级纯因子（设计 6.4.4）")

    class PricyLLM(MockLLM):
        def complete(self, prompt, *, model=None, temperature=0.0, **kw):
            r = super().complete(prompt, model=model, temperature=temperature)
            r.cost_cny = 10.0
            return r

    tracker = CostTracker(daily_budget_cny=1.0, monthly_budget_cny=100.0)
    c2 = LLMClient(PricyLLM(latency_ms=0), tracker=tracker, cache_enabled=False)
    raised = False
    try:
        c2.complete("SYMBOL: TEST\nx")
    except LLMBudgetExceeded:
        raised = True
    check("超预算抛 LLMBudgetExceeded", raised)

    tracker2 = CostTracker(daily_budget_cny=0.001, monthly_budget_cny=100.0)
    c3 = LLMClient(PricyLLM(latency_ms=0), tracker=tracker2, cache_enabled=False)
    g2 = BrainGraph(st, hub, c3, use_llm=True, max_workers=1)
    r2 = g2.run(cs, snap, symbols=cs.shortlist(3))
    check("熔断后系统不崩且仍产出", r2.n >= 0 and len(r2.states) > 0,
          f"n={r2.n} degraded={len(set(r2.degraded))}")
    check("熔断被记录为降级", any("预算" in d for d in r2.degraded),
          f"{sorted(set(r2.degraded))[:2]}")

    # -------------------------------------------------------- 10 缓存与解析
    logger.info("\n[10] LLM 缓存与鲁棒解析")
    c4 = LLMClient(MockLLM(latency_ms=0), tracker=CostTracker())
    p = "SYMBOL: 600000.SH\nunique-probe-xyz"
    a = c4.complete(p)
    b = c4.complete(p)
    check("同 prompt 命中缓存", b.cached and a.content == b.content)
    check("解析裸 JSON", parse_json_loose('{"score":0.8}').get("score") == 0.8)
    check("解析带 ``` 包裹", parse_json_loose('```json\n{"score":0.6}\n```').get("score") == 0.6)
    check("解析带前后缀文本",
          parse_json_loose('分析如下：{"score":0.4} 完毕').get("score") == 0.4)
    check("垃圾输入返回空 dict", parse_json_loose("这不是JSON") == {})
    check("None 安全", parse_json_loose("") == {})
    check("数字抽取含百分号", 0.15 in extract_numbers("涨幅 15% 明显"))

    # ------------------------------------------------------ 11 辩论轮次控制
    logger.info("\n[11] 辩论动态轮次（低一致性加轮，高一致性早停）")
    mod = DebateModerator(min_rounds=1, max_rounds=3)

    def mk_state(stances):
        s = AgentState(symbol="T", asof=asof, factpack=fp, portfolio=snap)
        from qmt_trade.brain.state import AnalystReport
        for i, (st_, cf) in enumerate(stances):
            s.reports[f"a{i}"] = AnalystReport(agent=f"a{i}", stance=st_,
                                               score=0.8 if st_ == "BULL" else 0.2,
                                               confidence=cf)
        return s

    hi = mk_state([("BULL", 0.9)] * 4)
    lo = mk_state([("BULL", 0.4), ("BEAR", 0.4), ("BULL", 0.4), ("BEAR", 0.4)])
    check("高一致性 → 最少轮次", mod.rounds_needed(hi) == 1, f"n={mod.rounds_needed(hi)}")
    check("低一致性 → 最多轮次", mod.rounds_needed(lo) == 3, f"n={mod.rounds_needed(lo)}")
    check("一致性度量正确", abs(hi.agreement - 1.0) < 1e-9 and abs(lo.agreement - 0.5) < 1e-9)
    check("辩论 token 可控（结构化）", len(hi.render_debate(max_turns=3)) < 2000)

    # --------------------------------------------------- 12 factcheck 边界
    logger.info("\n[12] TradeIntent 合法性校验（P3）")
    from qmt_trade.brain import TradeIntent
    good = TradeIntent(symbol="600000.SH", action="BUY", confidence=0.7,
                       conviction="HIGH", stop_loss_type="FIXED_PCT",
                       stop_loss_value=0.07, valid_until=asof + timedelta(days=3))
    check("合法 Intent 通过", check_intent(good, asof).ok)
    expired = good.model_copy(update={"valid_until": asof - timedelta(days=1)})
    check("过期 Intent 被拒", not check_intent(expired, asof).ok)
    huge = good.model_copy(update={"stop_loss_value": 0.9})
    check("止损过大被拒", not check_intent(huge, asof).ok)
    zero = good.model_copy(update={"stop_loss_value": 0.0})
    check("止损为零被拒", not check_intent(zero, asof).ok)
    sell = good.model_copy(update={"action": "SELL", "reasoning": "",
                                   "invalidation_checks": []})
    check("无依据的卖出被拒", not check_intent(sell, asof).ok)
    check("fingerprint 稳定", good.fingerprint() == good.model_copy().fingerprint())

    logger.info(f"\n{'=' * 60}\n结果: {PASS} 通过 / {FAIL} 失败\n{'=' * 60}")
    if r0.n:
        logger.info(r0.report())
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())