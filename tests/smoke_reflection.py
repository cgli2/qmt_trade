"""盘后自我复盘与总结的回归测试。

覆盖：
- ReflectionEngine 在「有问题」场景下的反思是否切中要害（牵强票、止损过紧、因子倒置、命中率低）；
- 改进策略是否联动 strategy 预设；
- 短期/长期记忆的合并（去重、频次累加、延续项标注）；
- LLM 增强失败安全回退；
- report 路由识别 reflection 种类、memory 序列化往返。
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from qmt_trade.evolution.review import (Lesson, ReviewResult, TradeAttribution)
from qmt_trade.evolution.reflection import (
    MemoryItem, ReflectionEngine, dump_long_term, load_long_term,
)


def _mk_review() -> ReviewResult:
    """构造一个"问题很明显"的复盘结果。"""
    atts = [
        TradeAttribution(symbol="000001.SZ", opened_at=date(2026, 8, 1),
                         closed_at=date(2026, 8, 11), holding_days=10,
                         entry=10.0, exit=9.2, shares=1000, gross_pnl=-800,
                         cost=120, net_pnl=-920, ret=-0.092,
                         reason="STOP_LOSS", conviction="HIGH", score=0.8),
        TradeAttribution(symbol="600000.SH", opened_at=date(2026, 8, 2),
                         closed_at=date(2026, 8, 11), holding_days=9,
                         entry=8.0, exit=7.6, shares=1000, gross_pnl=-400,
                         cost=100, net_pnl=-500, ret=-0.0625,
                         reason="STOP_LOSS", conviction="MED", score=0.6),
    ]
    lessons = [
        Lesson(date(2026, 8, 11), "STOP_TOO_TIGHT", "WARN",
               "止损触发占比过高", {"stop_ratio": 1.0},
               "提高 ATR 止损倍数，或改用结构位止损"),
        Lesson(date(2026, 8, 11), "COST_DRAG_HIGH", "WARN",
               "费用拖累过高", {"cost_drag": 0.012},
               "延长持有期或提高入选门槛，降低换手"),
        Lesson(date(2026, 8, 11), "FACTOR_INVERTED", "WARN",
               "因子 momentum_q 的 IC=-0.20 显著为负", {"factor": "momentum_q", "ic": -0.2},
               "下轮寻优中降权或反向验证"),
    ]
    stats = {
        "n": 2, "win_rate": 0.0, "profit_factor": 0.0, "net_pnl": -1420,
        "expectancy": -710, "avg_holding_days": 9.5, "cost_drag": 0.012,
        "best": -500, "worst": -920, "avg_win": 0.0, "avg_loss": -710,
    }
    return ReviewResult(asof=date(2026, 8, 11), attributions=atts, lessons=lessons,
                        stats=stats, factor_ic={"momentum_q": -0.2})


def _mk_picks() -> list[dict]:
    return [
        {"symbol": "000001.SZ", "rank": 1, "action": "BUY", "conviction": "HIGH",
         "confidence": 0.82, "industry": "银行",
         "bull_case": "估值低、资金流入", "bear_case": "基本面偏弱",
         "evidence": json.dumps([{"verdict": "bull", "factor": "value_q", "value": 0.8}])},
        # 牵强票：无多方论据、低置信、证据全中性
        {"symbol": "300750.SZ", "rank": 2, "action": "BUY", "conviction": "LOW",
         "confidence": 0.55, "industry": "电池",
         "bull_case": "", "bear_case": "产能过剩",
         "evidence": json.dumps([{"verdict": "neutral", "factor": "sent_q", "value": 0.5}])},
    ]


def _mk_trades() -> list[dict]:
    return [
        {"symbol": "000001.SZ", "side": "SELL", "price": 9.2, "volume": 1000,
         "amount": 9200, "total_cost": 60, "realized_pnl": -920},
        {"symbol": "600000.SH", "side": "SELL", "price": 7.6, "volume": 1000,
         "amount": 7600, "total_cost": 50, "realized_pnl": -500},
    ]


PASS = 0
FAIL = 0


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


def test_engine_problematic_scenario():
    eng = ReflectionEngine()
    rev = _mk_review()
    rep = eng.run(date(2026, 8, 11), review_result=rev, picks=_mk_picks(),
                  trades=_mk_trades(), regime="RANGE",
                  factor_ic={"momentum_q": -0.2},
                  selection_hit={"eval_days": 4, "hit_days": 1, "top_avg": 0.01, "all_avg": 0.02},
                  recent_experiences=["(WARN) 旧经验示例"],
                  short_term_prev=["昨日待办A"],
                  long_term_prev=[MemoryItem(text="因子 `momentum_q` 方向可能反了，长期应降权或反向验证。",
                                            tag="factor_ic", first_seen="2026-08-10",
                                            last_seen="2026-08-10", occurrences=1)])

    md = rep.to_markdown()
    for sec in ("一、当日概览", "二、选股反思", "三、交易反思", "四、改进策略",
                "五、经验总结", "六、短期记忆", "七、长期记忆"):
        check(sec in md, f"复盘 Markdown 缺少章节：{sec}")

    # 选股反思应点名牵强票
    joined = "\n".join(rep.selection_reflection)
    check("牵强" in joined, "选股反思未识别牵强票")
    check("300750.SZ" in joined, "选股反思未点名具体牵强标的")

    # 交易反思应涉及止损/费用
    tj = "\n".join(rep.trading_reflection)
    check(("止损" in tj) or ("费用拖累" in tj), "交易反思未触及止损/费用问题")

    # 改进策略应联动 strategy 预设（RANGE → value_quality）
    im = "\n".join(rep.improvements)
    check("value_quality" in im, "改进策略未联动 strategy 预设（RANGE→value_quality）")
    check("momentum_q" in im, "改进策略未对倒置因子提出降权")

    # 短期记忆非空，且包含延续项标注
    check(len(rep.short_term) > 0, "短期记忆为空")
    check("延续自昨日" in md, "复盘未标注延续自昨日的短期记忆")

    # 长期记忆：因子倒置原则应被合并且频次+1
    matched = [m for m in rep.long_term if "momentum_q" in m.text]
    check(len(matched) == 1, "长期记忆未正确合并因子倒置原则")
    if matched:
        check(matched[0].occurrences == 2, f"长期记忆频次未累加（实际 {matched[0].occurrences}）")

    # 长期记忆上限
    eng2 = ReflectionEngine()
    eng2.max_long = 5
    many = [MemoryItem(text=f"原则{i}", tag="t", first_seen="2026-08-01",
                       last_seen="2026-08-01", occurrences=1) for i in range(20)]
    merged = eng2._merge_long_term([(f"新{i}", "t") for i in range(10)], many, date(2026, 8, 11))
    check(len(merged) <= 5, f"长期记忆未封顶（实际 {len(merged)}）")


def test_engine_empty_scenario():
    eng = ReflectionEngine()
    rep = eng.run(date(2026, 8, 11), review_result=ReviewResult(asof=date(2026, 8, 11)))
    md = rep.to_markdown()
    check("## 二、选股反思" in md, "空场景缺少选股反思章节")
    check("无最终精选" in md, "空场景未说明无精选")
    check("无平仓" in md or "无可复盘" in md, "空场景未说明无交易")


class _FakeLLM:
    def __init__(self, payload: dict | None = None, fail: bool = False):
        self.payload = payload
        self.fail = fail
        self.called = False

    def complete(self, prompt, *, scene=None, temperature=None, tag=""):
        self.called = True
        if self.fail:
            raise RuntimeError("LLM down")
        import types
        resp = types.SimpleNamespace()
        resp.text = json.dumps(self.payload, ensure_ascii=False)
        return resp


def test_llm_enrich_and_fallback():
    rev = _mk_review()
    payload = {
        "selection_reflection": ["[LLM] 选股反思改写"],
        "trading_reflection": ["[LLM] 交易反思改写"],
        "improvements": ["[LLM] 改进改写"],
        "lessons_summary": ["[LLM] 经验改写"],
    }
    eng = ReflectionEngine()
    rep = eng.run(date(2026, 8, 11), review_result=rev, picks=_mk_picks(),
                  regime="RANGE", llm=_FakeLLM(payload))
    check(rep.llm_used is True, "LLM 增强未标记 llm_used")
    check(rep.selection_reflection and rep.selection_reflection[0].startswith("[LLM]"),
          "LLM 改写未生效")

    # 失败回退：llm_used 仍为 False，规则结果保留
    rep2 = eng.run(date(2026, 8, 11), review_result=rev, picks=_mk_picks(),
                   regime="RANGE", llm=_FakeLLM(fail=True))
    check(rep2.llm_used is False, "LLM 失败未回退为规则引擎")
    check(len(rep2.selection_reflection) > 0, "LLM 失败回退后反思为空")


def test_memory_serialization():
    items = [MemoryItem(text="A", tag="t", first_seen="2026-08-01",
                        last_seen="2026-08-01", occurrences=2),
             MemoryItem(text="B", tag="t", first_seen="2026-08-02",
                        last_seen="2026-08-02", occurrences=1)]
    raw = dump_long_term(items)
    back = load_long_term(raw)
    check(len(back) == 2, "长期记忆反序列化数量不符")
    check(back[0].text == "A" and back[0].occurrences == 2, "长期记忆字段丢失")
    check(load_long_term(None) == [], "空输入未返回空列表")


def test_report_router_recognizes_reflection():
    from server.routers import report as rp
    check("reflection" in rp._KIND_PAT, "report 路由未注册 reflection 种类")
    m = rp._KIND_PAT["reflection"].match("reflection_20260811.md")
    check(bool(m) and m.group(1) == "20260811", "reflection 文件名正则不匹配")
    check(rp._KIND_PAT["reflection"].match("daily_20260811.md") is None,
          "reflection 正则误匹配其他报告")
    # content 接口允许该文件名（防路径穿越名单覆盖）
    allowed = any(p.match("reflection_20260811.md") for p in rp._KIND_PAT.values())
    check(allowed, "report/content 未放行 reflection 文件")


def test_self_reflect_integration():
    """端到端：JobRunner._self_reflect 真实落盘 md + 记忆（不依赖 provider/LLM）。"""
    import tempfile
    from pathlib import Path

    from qmt_trade.scheduler.jobs import JobRunner
    from qmt_trade.storage.db import Database
    from qmt_trade.storage.models import Repos
    from qmt_trade.core.clock import TradingCalendar

    tmp = Path(tempfile.mkdtemp(prefix="reflect_"))
    db = Database(tmp / "t.db")
    repos = Repos.create(db)
    out_dir = tmp / "reports"

    class FakeBrain:
        pass  # 无 client 属性 → llm=None（纯规则）

    class FakeReporter:
        output_dir = out_dir

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.settings = None
    ctx.shared_repos = repos
    ctx.repos = repos
    ctx.brain = FakeBrain()
    ctx.reporter = FakeReporter()
    ctx.calendar = TradingCalendar()

    runner = JobRunner(ctx)
    d = date(2026, 8, 11)
    rev = _mk_review()
    res = runner._self_reflect(d, rev)

    check(res is not None, "端到端 _self_reflect 返回 None（异常）")
    if res:
        md_path = out_dir / f"reflection_{d:%Y%m%d}.md"
        check(md_path.exists(), "reflection_*.md 未落盘")
        if md_path.exists():
            txt = md_path.read_text(encoding="utf-8")
            check("盘后自我复盘与总结" in txt, "落盘 md 内容异常")
        # 记忆落库
        st = repos.system.get(f"reflection:short_term:{d.isoformat()}")
        lt = repos.system.get("reflection:long_term")
        check(st is not None, "短期记忆未落库")
        check(lt is not None, "长期记忆未落库")
        if lt:
            lt_items = json.loads(lt)
            check(isinstance(lt_items, list) and len(lt_items) > 0, "长期记忆为空")


if __name__ == "__main__":
    test_engine_problematic_scenario()
    test_engine_empty_scenario()
    test_llm_enrich_and_fallback()
    test_memory_serialization()
    test_report_router_recognizes_reflection()
    test_self_reflect_integration()
    print(f"\n反射复盘测试通过 {PASS} / 失败 {FAIL}")
    sys.exit(1 if FAIL else 0)
