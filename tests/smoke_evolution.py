"""L5 进化层冒烟测试：复盘归因 / walk-forward 寻优 / 策略池调权。

重点验证「这三个模块不是死代码」：

- ReviewEngine 能直接消费回测产出的 ``closed_trades``（真实闭环，非造数据）；
- 经验条目在设定的病态输入下**必然**被触发（止损太紧/费用拖累/conviction 倒挂）；
- WalkForwardOptimizer 的三道防过拟合闸门在该拒绝时确实拒绝；
- StrategyPool 的影子期、隔离、退休、权重限幅逐条生效，权重恒和为 1。
"""

from __future__ import annotations
import logging

import math
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from qmt_trade.core.config import Settings  # noqa: E402
from qmt_trade.evolution import (CASH, Lesson, ReviewEngine, StrategyPool,  # noqa: E402
                                 WalkForwardOptimizer)

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


def tags(lessons: list[Lesson]) -> set[str]:
    return {x.tag for x in lessons}


# ==================================================================== 复盘
def test_review(st: Settings) -> None:
    logger.info("\n[1] 复盘归因 —— 正常样本")
    eng = ReviewEngine(st)
    d0 = date(2026, 6, 1)
    log = []
    for i in range(12):
        win = i % 3 != 0                       # 8 胜 4 负
        entry, shares = 10.0, 1000
        exit_ = entry * (1.03 if win else 0.96)
        cost = 12.0
        log.append({
            "symbol": f"{600000 + i}.SH", "entry_price": entry, "exit_price": exit_,
            "shares": shares, "cost": cost, "pnl": (exit_ - entry) * shares - cost,
            "opened_at": d0 + timedelta(days=i), "closed_at": d0 + timedelta(days=i + 5),
            "holding_days": 5, "reason": "SIGNAL" if win else "STOP_LOSS",
            "conviction": "HIGH" if win else "LOW",
        })
    res = eng.run(date(2026, 7, 1), log)
    logger.info(res.report())
    check("归因笔数正确", res.n == 12, f"n={res.n}")
    check("胜率计算正确", abs(res.stats["win_rate"] - 8 / 12) < 1e-9,
          f"win_rate={res.stats['win_rate']:.3f}")
    check("盈亏比 > 1", res.stats["profit_factor"] > 1.0,
          f"pf={res.stats['profit_factor']:.2f}")
    check("净盈亏 = 逐笔之和",
          abs(res.stats["net_pnl"] - sum(a.net_pnl for a in res.attributions)) < 1e-6)
    check("期望值自洽",
          abs(res.stats["expectancy"] - res.stats["net_pnl"] / 12) < 1e-9)
    check("平仓原因被保留", {a.reason for a in res.attributions} == {"SIGNAL", "STOP_LOSS"})

    logger.info("\n[2] 复盘 —— 样本不足时不下结论（防小样本吹牛）")
    small = eng.run(date(2026, 7, 1), log[:3])
    check("触发 SAMPLE_TOO_SMALL", "SAMPLE_TOO_SMALL" in tags(small.lessons))
    check("样本不足时不产出其它结论", len(small.lessons) == 1,
          f"lessons={len(small.lessons)}")

    logger.info("\n[3] 复盘 —— 病态输入必须报警")
    bad = []
    for i in range(20):
        stop = i < 15                          # 75% 都是止损出局
        entry, shares = 20.0, 500
        exit_ = entry * (0.97 if stop else 1.01)
        bad.append({
            "symbol": f"{300000 + i}.SZ", "entry_price": entry, "exit_price": exit_,
            "shares": shares, "cost": 200.0,   # 刻意抬高费用
            "pnl": (exit_ - entry) * shares - 200.0,
            "holding_days": 2, "reason": "STOP_LOSS" if stop else "SIGNAL",
            "conviction": "HIGH" if stop else "LOW",   # 刻意倒挂
        })
    res2 = eng.run(date(2026, 7, 2), bad)
    logger.info(res2.report())
    t = tags(res2.lessons)
    check("识别止损过紧", "STOP_TOO_TIGHT" in t, str(sorted(t)))
    check("识别费用拖累过高", "COST_DRAG_HIGH" in t)
    check("识别 conviction 分档失效", "CONVICTION_INVERTED" in t)
    check("CRITICAL 级别被正确标注",
          any(x.severity == "CRITICAL" for x in res2.lessons))

    logger.info("\n[4] 复盘 —— 止盈过早（高胜率低盈亏比）")
    early = []
    for i in range(20):
        win = i < 14                           # 70% 胜率
        entry, shares = 15.0, 400
        exit_ = entry * (1.005 if win else 0.95)   # 赢一点点，输一大截
        early.append({"symbol": f"{1000 + i}.SZ", "entry_price": entry,
                      "exit_price": exit_, "shares": shares, "cost": 5.0,
                      "pnl": (exit_ - entry) * shares - 5.0, "holding_days": 3,
                      "reason": "SIGNAL"})
    res3 = eng.run(date(2026, 7, 3), early)
    check("识别盈利单被过早了结", "CUT_WINNERS_EARLY" in tags(res3.lessons),
          f"pf={res3.stats['profit_factor']:.2f} wr={res3.stats['win_rate']:.0%}")


# ================================================================ 因子 IC
def test_factor_ic(st: Settings) -> None:
    logger.info("\n[5] 因子 IC —— 正向/反向因子都要能识别")
    import pandas as pd
    syms = [f"{600100 + i}.SH" for i in range(20)]
    frame = pd.DataFrame({
        "symbol": syms,
        "cat_good": [i / 20 for i in range(20)],          # 与收益正相关
        "cat_bad": [1 - i / 20 for i in range(20)],       # 与收益负相关
        "score": [i / 20 for i in range(20)],
    })
    fwd = {s: (i - 10) * 0.002 for i, s in enumerate(syms)}
    eng = ReviewEngine(st)
    res = eng.run(date(2026, 7, 4), [], factor_frame=frame, forward_returns=fwd)
    ic = res.factor_ic
    check("正向因子 IC 接近 +1", ic.get("cat_good", 0) > 0.9, f"ic={ic.get('cat_good')}")
    check("反向因子 IC 接近 -1", ic.get("cat_bad", 0) < -0.9, f"ic={ic.get('cat_bad')}")
    res.factor_ic = ic
    lessons = eng._lessons(date(2026, 7, 4), res)
    check("反向因子触发 FACTOR_INVERTED",
          any(x.tag == "FACTOR_INVERTED" for x in lessons))


# ============================================================ walk-forward
def test_optimizer(st: Settings) -> None:
    logger.info("\n[6] Walk-forward —— 窗口切分")
    calls: list[tuple] = []

    def evaluate(params, s, e):
        calls.append((tuple(sorted(params.items())), s, e))
        return 0.0

    opt = WalkForwardOptimizer(evaluate, st, train_days=60, valid_days=30, step_days=30,
                               min_windows=2)
    wins = opt.make_windows(date(2025, 1, 1), date(2026, 1, 1))
    check("产出多个窗口", len(wins) >= 3, f"n={len(wins)}")
    check("训练窗在验证窗之前",
          all(w.train_end < w.valid_start <= w.valid_end for w in wins))
    check("验证窗不越界", all(w.valid_end <= date(2026, 1, 1) for w in wins))
    check("窗口按步长滚动",
          all((wins[i + 1].train_start - wins[i].train_start).days == 30
              for i in range(len(wins) - 1)))
    check("相邻窗口的验证段不重叠",
          all(wins[i].valid_start > wins[i - 1].valid_start for i in range(1, len(wins))))

    logger.info("\n[7] Walk-forward —— 稳健参数胜出")
    # A：样本外稳定 0.9；B：训练窗巨好但验证窗忽正忽负（典型过拟合）
    def eval2(params, s, e):
        is_train = (e - s).days > 45
        if params["k"] == "A":
            return 1.0 if is_train else 0.9
        return 3.0 if is_train else (1.6 if s.month % 2 == 0 else -1.2)

    opt2 = WalkForwardOptimizer(eval2, st, train_days=60, valid_days=30, step_days=30,
                                min_windows=2)
    r2 = opt2.run({"k": ["A", "B"]}, date(2025, 1, 1), date(2026, 1, 1))
    logger.info(r2.report())
    check("稳健参数 A 胜出", r2.best is not None and r2.best.params["k"] == "A",
          f"best={r2.best.params if r2.best else None}")
    b = next(p for p in r2.all_scores if p.params["k"] == "B")
    check("过拟合参数 B 的训练-验证差更大", b.overfit_gap > r2.best.overfit_gap,
          f"B={b.overfit_gap:.2f} A={r2.best.overfit_gap:.2f}")
    check("B 的样本外波动更大", b.std_oos > r2.best.std_oos,
          f"B={b.std_oos:.2f} A={r2.best.std_oos:.2f}")

    logger.info("\n[8] Walk-forward —— 三道闸门")
    opt3 = WalkForwardOptimizer(lambda p, s, e: 1.0, st, train_days=60, valid_days=30,
                                step_days=30, min_windows=99)
    r3 = opt3.run({"k": [1]}, date(2025, 1, 1), date(2026, 1, 1))
    check("窗口不足时拒绝给结论", not r3.accepted and "窗口" in r3.reason, r3.reason)

    def eval_overfit(params, s, e):
        return 5.0 if (e - s).days > 45 else 0.2

    opt4 = WalkForwardOptimizer(eval_overfit, st, train_days=60, valid_days=30,
                                step_days=30, min_windows=2)
    r4 = opt4.run({"k": [1]}, date(2025, 1, 1), date(2026, 1, 1))
    check("过拟合被拒绝", not r4.accepted and "过拟合" in r4.reason, r4.reason)

    def eval_flat(params, s, e):
        return 1.0 if params["k"] == 1 else 1.02   # 仅提升 2%，不值得换

    opt5 = WalkForwardOptimizer(eval_flat, st, train_days=60, valid_days=30,
                                step_days=30, min_windows=2)
    r5 = opt5.run({"k": [1, 2]}, date(2025, 1, 1), date(2026, 1, 1), baseline={"k": 1})
    check("微小提升不换参", not r5.accepted and "门槛" in r5.reason, r5.reason)

    def eval_big(params, s, e):
        return 0.5 if params["k"] == 1 else 1.5    # 提升 3 倍，值得换

    opt6 = WalkForwardOptimizer(eval_big, st, train_days=60, valid_days=30,
                                step_days=30, min_windows=2)
    r6 = opt6.run({"k": [1, 2]}, date(2025, 1, 1), date(2026, 1, 1), baseline={"k": 1})
    check("显著提升才换参", r6.accepted and r6.best.params["k"] == 2, r6.reason)

    logger.info("\n[9] Walk-forward —— 单窗评估失败不毁全局 + 变更限幅")
    def eval_flaky(params, s, e):
        if s.month == 3:
            raise RuntimeError("数据缺失")
        return 1.0

    opt7 = WalkForwardOptimizer(eval_flaky, st, train_days=60, valid_days=30,
                                step_days=30, min_windows=2)
    r7 = opt7.run({"k": [1]}, date(2025, 1, 1), date(2026, 1, 1))
    check("部分窗口失败仍能出结果", r7.best is not None and len(r7.best.oos_scores) > 0,
          f"有效窗口={len(r7.best.oos_scores) if r7.best else 0}/{len(r7.windows)}")

    clamped = WalkForwardOptimizer.clamp_change(
        {"stop": 0.05, "top_n": 10, "mode": "A"},
        {"stop": 0.20, "top_n": 40, "mode": "B"}, max_step=0.3)
    check("浮点参数被限幅在 ±30%", abs(clamped["stop"] - 0.065) < 1e-9,
          f"stop={clamped['stop']:.4f}")
    check("整数参数限幅后仍是整数",
          isinstance(clamped["top_n"], int) and clamped["top_n"] == 13,
          f"top_n={clamped['top_n']}")
    check("非数值参数直接透传", clamped["mode"] == "B")
    down = WalkForwardOptimizer.clamp_change({"stop": 0.10}, {"stop": 0.01}, max_step=0.3)
    check("向下调整同样被限幅", abs(down["stop"] - 0.07) < 1e-9, f"stop={down['stop']:.4f}")


# ================================================================= 策略池
def test_pool(st: Settings) -> None:
    logger.info("\n[10] 策略池 —— 影子期不给钱")
    pool = StrategyPool(st)
    pool.register("alpha_mom")
    pool.record_batch("alpha_mom", [0.004] * 30)     # 未达 promote_min_obs=40
    r = pool.rebalance(date(2026, 7, 1))
    check("影子策略权重为 0", r.weights["alpha_mom"] == 0.0)
    check("资金全在现金", abs(r.weights[CASH] - 1.0) < 1e-9)
    check("状态仍为 SHADOW", pool.strategies["alpha_mom"].status == "SHADOW")

    logger.info("\n[11] 策略池 —— 达标转正并分到资金")
    pool.record_batch("alpha_mom", [0.004, -0.001] * 10)   # 累计 50 个样本
    r = pool.rebalance(date(2026, 7, 8))
    logger.info(r.report())
    check("影子转 ACTIVE", pool.strategies["alpha_mom"].status == "ACTIVE")
    check("分到资金但受单次限幅", 0 < r.weights["alpha_mom"] <= pool.max_step + 1e-9,
          f"w={r.weights['alpha_mom']:.2%}")
    check("权重和为 1", abs(sum(r.weights.values()) - 1.0) < 1e-6,
          f"sum={sum(r.weights.values()):.6f}")

    logger.info("\n[12] 策略池 —— 权重逐次爬升到上限，不跳变")
    prev = r.weights["alpha_mom"]
    for i in range(6):
        pool.record_batch("alpha_mom", [0.005, 0.003])
        rr = pool.rebalance(date(2026, 7, 15) + timedelta(days=7 * i))
        cur = rr.weights["alpha_mom"]
        if cur - prev > pool.max_step + 1e-9:
            check("单次变动不超过 max_step", False, f"{prev:.2%}→{cur:.2%}")
            break
        prev = cur
    else:
        check("单次变动不超过 max_step", True, f"最终 w={prev:.2%}")
    check("未突破单策略上限", prev <= pool.max_weight + 1e-9,
          f"w={prev:.2%} cap={pool.max_weight:.0%}")

    logger.info("\n[13] 策略池 —— 崩了要隔离，连续不合格要退休")
    pool2 = StrategyPool(st)
    pool2.register("alpha_bad", status="ACTIVE")
    pool2.record_batch("alpha_bad", [0.01] * 25)
    pool2.rebalance(date(2026, 8, 1))
    pool2.record_batch("alpha_bad", [-0.02] * 30)          # 连续大跌
    r = pool2.rebalance(date(2026, 8, 8))
    check("触发隔离", pool2.strategies["alpha_bad"].status == "QUARANTINE",
          pool2.strategies["alpha_bad"].status)
    check("隔离后不给钱", r.weights["alpha_bad"] == 0.0)
    for i in range(pool2.retire_after):
        pool2.record_batch("alpha_bad", [-0.02] * 5)
        r = pool2.rebalance(date(2026, 8, 15) + timedelta(days=7 * i))
    check("连续不合格后退休", pool2.strategies["alpha_bad"].status == "RETIRED",
          pool2.strategies["alpha_bad"].status)
    check("退休后资金回到现金", abs(r.weights[CASH] - 1.0) < 1e-9)

    logger.info("\n[14] 策略池 —— 隔离后恢复可以解除")
    pool3 = StrategyPool(st)
    pool3.register("alpha_rec", status="ACTIVE")
    pool3.record_batch("alpha_rec", [-0.02] * 25)
    pool3.rebalance(date(2026, 8, 1))
    check("先进入隔离", pool3.strategies["alpha_rec"].status == "QUARANTINE")
    pool3.record_batch("alpha_rec", [0.02] * 60)           # 强势恢复
    pool3.rebalance(date(2026, 8, 8))
    check("恢复后解除隔离", pool3.strategies["alpha_rec"].status == "ACTIVE",
          pool3.strategies["alpha_rec"].status)

    logger.info("\n[15] 策略池 —— 多策略按得分正比分配且不超上限")
    pool4 = StrategyPool(st)
    # 同样的平均收益，风险不同 → 风险调整后得分必须拉开差距
    profiles = {
        "s_hi": [0.004, 0.003],            # 稳
        "s_mid": [0.010, -0.003],          # 波动大
        "s_lo": [0.020, -0.013],           # 波动更大
    }
    for name, pat in profiles.items():
        pool4.register(name, status="ACTIVE")
        pool4.record_batch(name, pat * 30)
    for i in range(8):                                     # 多轮调权让权重收敛
        r = pool4.rebalance(date(2026, 9, 1) + timedelta(days=7 * i))
    logger.info(r.report())
    w = r.weights
    check("得分高的拿得多", w["s_hi"] >= w["s_mid"] >= w["s_lo"],
          f"{w['s_hi']:.2%}/{w['s_mid']:.2%}/{w['s_lo']:.2%}")
    check("单策略不超上限", max(w[k] for k in ("s_hi", "s_mid", "s_lo")) <= pool4.max_weight + 1e-9)
    check("权重和恒为 1", abs(sum(w.values()) - 1.0) < 1e-6, f"sum={sum(w.values()):.6f}")
    check("无负权重", all(v >= 0 for v in w.values()))

    logger.info("\n[16] 策略池 —— 快照可复现（P6）")
    snap = pool4.snapshot()
    pool5 = StrategyPool(st)
    pool5.load(snap)
    check("状态可完整还原",
          all(pool5.strategies[k].status == pool4.strategies[k].status
              and abs(pool5.strategies[k].weight - pool4.strategies[k].weight) < 1e-12
              for k in pool4.strategies))
    check("保留名受保护",
          _raises(lambda: pool5.register(CASH)))


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


# ======================================================== 与回测的真实闭环
def test_closed_loop(st: Settings) -> None:
    logger.info("\n[17] 闭环 —— 复盘直接消费回测产出的平仓明细")
    from qmt_trade.backtest import BacktestEngine
    from qmt_trade.datahub.manager import DataHub
    from qmt_trade.datahub.providers.mock import MockProvider

    s2 = Settings.load()
    for k in ("bars", "fundamentals", "news"):
        s2.set(f"datahub.priority.{k}", ["mock"])
    s2.set("selection.ranker.top_n", 10)
    s2.set("selection.ranker.max_per_industry", 3)
    hub = DataHub(s2, [MockProvider(n_symbols=25, start="2025-06-02", end="2026-08-07", seed=11)])
    eng = BacktestEngine(s2, hub, initial_cash=1_000_000, top_n=5, max_holding_days=10)
    res = eng.run(date(2026, 4, 1), date(2026, 6, 30))

    check("回测产出平仓明细", len(res.closed_trades) > 0, f"n={len(res.closed_trades)}")
    if res.closed_trades:
        keys = set(res.closed_trades[0])
        check("明细字段完整",
              {"symbol", "entry_price", "exit_price", "shares", "pnl", "reason",
               "holding_days"} <= keys, str(sorted(keys)))
        check("平仓原因是机器可读的枚举",
              all(t["reason"] in {"STOP_LOSS", "TIME_STOP", "TRAILING", "FLATTEN",
                                  "SIGNAL", "REBALANCE"} for t in res.closed_trades),
              str({t["reason"] for t in res.closed_trades}))

    rev = ReviewEngine(s2).run(date(2026, 6, 30), res.closed_trades)
    logger.info(rev.report())
    check("复盘能直接跑通", rev.n == len(res.closed_trades))
    if rev.n:
        tot = sum(a.net_pnl for a in rev.attributions)
        check("归因盈亏与回测已实现盈亏一致",
              abs(tot - sum(eng.portfolio.realized_log)) < 1e-6,
              f"review={tot:.2f} portfolio={sum(eng.portfolio.realized_log):.2f}")
        check("统计量为有限值",
              all(math.isfinite(v) for v in rev.stats.values()
                  if isinstance(v, float) and v != float("inf")))


def main() -> int:
    st = Settings.load()
    test_review(st)
    test_factor_ic(st)
    test_optimizer(st)
    test_pool(st)
    test_closed_loop(st)
    logger.info("\n" + "=" * 46)
    logger.info(f"通过 {PASS} / 失败 {FAIL}")
    logger.info("=" * 46)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())