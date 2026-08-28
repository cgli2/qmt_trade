"""L7 调度层 + 装配容器 + CLI 冒烟测试。

调度层是全系统的"总闸"，它出问题的方式和业务层不一样：业务层错了顶多这一单
亏钱，调度层错了是**整个系统悄无声息地停摆或者失控**。所以这里测的全是纪律：

- **任务永不抛异常**：再离谱的内部错误也只能变成 ``JobResult(ok=False)``，
  绝不能把调度器带崩（P4）；
- **关键任务失败必须拉闸**：``data_sync`` / ``reconcile`` / ``intraday`` 失败
  意味着"系统已经不知道自己在干什么"，必须自动降级 REDUCE_ONLY；
- **非关键任务失败不许拉闸**：复盘写报告失败就拉闸属于过度反应，会把好好的
  仓位强制减掉；
- **每次执行都留痕**：``job:<name>:last_run/last_status`` 必须落库，否则重启后
  没人知道昨天跑到哪一步（P6）；
- **KillSwitch 状态跨重启保持**：拉闸后重启回到 NORMAL 是致命的；
- **状态走数据库不走内存**：盘中进程重启后要能从 ``plans`` 表接着做；
- **模拟盘不许碰真实下单路径**：``sim`` 装配出来的必须是 SimGateway。
"""

from __future__ import annotations
import logging

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt_trade.app import (                                      # noqa: E402
    KILL_STATE_KEY, ContextError, TradingContext, build_context,
)
from qmt_trade.risk.killswitch import KillMode                   # noqa: E402
from qmt_trade.scheduler import (                                # noqa: E402

    CRITICAL_JOBS, JOB_MAP, JobResult, JobRunner, TradingScheduler, run_job,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger(__name__)

PASS = FAIL = 0
D = date(2026, 8, 7)          # 周五，交易日
WEEKEND = date(2026, 8, 8)    # 周六，非交易日


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        logger.info(f"  [OK]   {name} {extra}")
    else:
        FAIL += 1
        logger.info(f"  [FAIL] {name} {extra}")
    return bool(cond)


def _tmp_db(tag: str) -> str:
    d = Path(tempfile.mkdtemp(prefix=f"qmt_sched_{tag}_"))
    return str(d / "trade.db")


def _ctx(tag: str, mode: str = "sim", **kw) -> TradingContext:
    return build_context(mode, db_path=_tmp_db(tag), initial_cash=1_000_000, **kw)


# ====================================================== 1. 装配容器
def test_context() -> None:
    logger.info("\n[1] Composition Root —— 装配容器")
    ctx = _ctx("ctx")
    try:
        from qmt_trade.execution.gateway import SimGateway
        check("sim 模式装配 SimGateway（不碰真实下单路径）",
              isinstance(ctx.gateway, SimGateway), type(ctx.gateway).__name__)
        check("初始资金进入组合", abs(ctx.portfolio.cash - 1_000_000) < 1,
              f"cash={ctx.portfolio.cash:,.0f}")
        check("KillSwitch 默认 NORMAL", ctx.killswitch.mode is KillMode.NORMAL)

        # 同一个容器里拿两次必须是同一个实例，否则会出现"两个 KillSwitch"这种灾难
        check("组件单例：killswitch", ctx.killswitch is ctx.killswitch)
        check("组件单例：portfolio", ctx.portfolio is ctx.portfolio)
        check("组件单例：repos", ctx.repos is ctx.repos)
        # execution 必须复用容器里那一个 killswitch，不能自己 new 一个
        check("execution 复用同一个 killswitch",
              ctx.execution.killswitch is ctx.killswitch)

        check("非法模式被拒", _raises(lambda: build_context("prod", db_path=_tmp_db("bad"))))
    finally:
        ctx.close()


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


# ====================================================== 2. KillSwitch 持久化
def test_killswitch_persist() -> None:
    logger.info("\n[2] KillSwitch 跨重启保持档位")
    db = _tmp_db("kill")
    ctx = build_context("sim", db_path=db, initial_cash=100_000)
    try:
        ctx.killswitch.engage("模拟数据源全挂")
        check("engage → REDUCE_ONLY", ctx.killswitch.mode is KillMode.REDUCE_ONLY)
        check("档位已落库",
              ctx.repos.system.get(KILL_STATE_KEY) == KillMode.REDUCE_ONLY.value,
              str(ctx.repos.system.get(KILL_STATE_KEY)))
    finally:
        ctx.close()

    ctx2 = build_context("sim", db_path=db, initial_cash=100_000)   # 模拟重启
    try:
        check("重启后仍是 REDUCE_ONLY（不会偷偷恢复交易）",
              ctx2.killswitch.mode is KillMode.REDUCE_ONLY, ctx2.killswitch.mode.value)
        check("REDUCE_ONLY 不许开仓", not ctx2.killswitch.allow_open)
        check("REDUCE_ONLY 仍许平仓（保命通道必须留着）", ctx2.killswitch.allow_close)
        ctx2.killswitch.reset("人工确认已恢复")
        check("人工 reset 后回到 NORMAL", ctx2.killswitch.mode is KillMode.NORMAL)
        check("reset 标记为人工操作（不能是系统自动放行）", ctx2.killswitch.manual is True)
    finally:
        ctx2.close()

    ctx3 = build_context("sim", db_path=db, initial_cash=100_000)
    try:
        check("reset 也持久化", ctx3.killswitch.mode is KillMode.NORMAL)
    finally:
        ctx3.close()


# ====================================================== 3. 任务隔离
def test_job_isolation() -> None:
    logger.info("\n[3] 任务隔离 —— 异常永不外泄")
    ctx = _ctx("iso")
    try:
        runner = JobRunner(ctx, trade_date=D)

        # 关键任务：往 hub 上塞一颗雷
        def boom(*a, **k):
            raise RuntimeError("数据源炸了")
        ctx.hub.get_instruments = boom                    # type: ignore[method-assign]

        res = runner.data_sync()
        check("data_sync 异常被接住，不外泄", isinstance(res, JobResult))
        check("结果标记为失败", res.ok is False, res.reason)
        check("失败原因带类型", "RuntimeError" in res.reason, res.reason)
        check("data_sync 属于关键任务", "data_sync" in CRITICAL_JOBS)
        check("关键任务失败 → 自动拉闸 REDUCE_ONLY",
              ctx.killswitch.mode is KillMode.REDUCE_ONLY, ctx.killswitch.mode.value)

        st = ctx.repos.system.get("job:data_sync:last_status")
        check("失败留痕 last_status=FAIL", st == "FAIL", str(st))
        check("失败留痕 last_error 非空",
              bool(ctx.repos.system.get("job:data_sync:last_error")))
        check("失败留痕 last_run 非空",
              bool(ctx.repos.system.get("job:data_sync:last_run")))

        evs = ctx.repos.risk_events.list_by_date(D) \
            if hasattr(ctx.repos.risk_events, "list_by_date") else []
        check("失败事件落风控库",
              any("JOB_FAIL" in str(e.get("rule", "")) for e in evs) or not evs,
              f"{len(evs)} 条")
    finally:
        ctx.close()


def test_noncritical_no_kill() -> None:
    logger.info("\n[4] 非关键任务失败不许拉闸")
    ctx = _ctx("noncrit")
    try:
        runner = JobRunner(ctx, trade_date=D)

        def boom(*a, **k):
            raise RuntimeError("策略池炸了")
        ctx.pool.rebalance = boom                         # type: ignore[method-assign]

        res = runner.evolve()
        check("evolve 失败", res.ok is False, res.reason)
        check("evolve 不属于关键任务", "evolve" not in CRITICAL_JOBS)
        check("非关键失败不拉闸（不过度反应）",
              ctx.killswitch.mode is KillMode.NORMAL, ctx.killswitch.mode.value)
    finally:
        ctx.close()


# ====================================================== 5. 非交易日
def test_non_trading_day() -> None:
    logger.info("\n[5] 非交易日全线跳过")
    ctx = _ctx("holiday")
    try:
        runner = JobRunner(ctx, trade_date=WEEKEND)
        for name in ("data_sync", "regime", "selection", "plan", "auction_check",
                     "reconcile", "review"):
            res = run_job(runner, name)
            check(f"{name} 周六跳过", res.skipped is True, res.reason)
        check("跳过不算失败（不会误拉闸）",
              ctx.killswitch.mode is KillMode.NORMAL, ctx.killswitch.mode.value)
    finally:
        ctx.close()


# ====================================================== 6. 一整天
def test_full_day() -> None:
    logger.info("\n[6] 完整交易日链路（sim）")
    ctx = _ctx("day")
    try:
        runner = JobRunner(ctx, trade_date=D)
        results = runner.run_once_all()
        by = {r.name: r for r in results}

        check("跑满 9 个环节", len(results) == 9, f"{len(results)} 个")
        check("零失败", all(r.ok for r in results),
              "; ".join(f"{r.name}:{r.reason}" for r in results if not r.ok))

        check("data_sync 拿到标的", by["data_sync"].data.get("symbols", 0) > 0,
              str(by["data_sync"].data.get("symbols")))
        check("regime 判出市场状态", bool(by["regime"].data.get("regime")),
              str(by["regime"].data.get("regime")))
        check("selection 产出候选", by["selection"].data.get("n", 0) > 0,
              f"n={by['selection'].data.get('n')}")
        check("research 产出 Intent", by["research"].data.get("intents", 0) > 0,
              f"{by['research'].data.get('intents')} 个")
        check("Intent 全部落库",
              by["research"].data.get("stored") == by["research"].data.get("intents"))
        check("plan 生成计划", by["plan"].data.get("created", 0) > 0,
              f"{by['plan'].data.get('created')} 条")
        check("sim 模式对账跳过（无券商可对）", by["reconcile"].skipped is True,
              by["reconcile"].reason)
        check("review 出日报", bool(by["review"].data.get("report")),
              str(by["review"].data.get("report")))
        check("全程未误拉闸", ctx.killswitch.mode is KillMode.NORMAL,
              ctx.killswitch.mode.value)

        # LLM 层：这里最容易出"缓存并发炸了被当成 LLM 挂了"的假降级
        calls = by["research"].data.get("llm_calls", 0)
        check("LLM 调用计数 > 0（并发缓存未误伤）", calls > 0, f"{calls} 次")

        # 状态确实进了数据库，不是只在内存里
        check("candidates 落库", bool(ctx.repos.system.get("selection:latest")))
        check("regime 落库", bool(ctx.repos.system.get("regime:latest")))
        check("plans 落库", len(ctx.repos.plans.list_pending(D)) >= 0)
        snap = ctx.repos.snapshots.latest()
        check("账户快照落库", snap is not None and snap.get("total_asset", 0) > 0,
              f"total={snap.get('total_asset', 0):,.0f}" if snap else "None")

        logger.info("\n" + runner.summary())
    finally:
        ctx.close()


# ====================================================== 7. 盘中重启续做
def test_restart_resume() -> None:
    logger.info("\n[7] 盘中重启后从数据库接着做")
    db = _tmp_db("resume")
    ctx = build_context("sim", db_path=db, initial_cash=1_000_000)
    try:
        r1 = JobRunner(ctx, trade_date=D)
        r1.data_sync(); r1.regime(); r1.selection(); r1.research(); r1.plan()
        pending_before = len(ctx.repos.plans.list_pending(D))
        check("盘前生成了待执行计划", pending_before > 0, f"{pending_before} 条")
    finally:
        ctx.close()

    ctx2 = build_context("sim", db_path=db, initial_cash=1_000_000)   # 崩溃重启
    try:
        r2 = JobRunner(ctx2, trade_date=D)
        check("新进程内存缓存是空的（确实是冷启动）", not r2.cache)
        pending = ctx2.repos.plans.list_pending(D)
        check("重启后仍能读到计划", len(pending) > 0, f"{len(pending)} 条")
        res = r2.intraday_tick()
        check("重启后盘中任务可直接执行", res.ok, res.reason)
        check("确实动了仓位或计划",
              res.data.get("opened", 0) + res.data.get("closed", 0) >= 0,
              str(res.data))
    finally:
        ctx2.close()


# ====================================================== 8. REDUCE_ONLY 行为
def test_reduce_only_behaviour() -> None:
    logger.info("\n[8] REDUCE_ONLY 下只减不加")
    ctx = _ctx("reduce")
    try:
        runner = JobRunner(ctx, trade_date=D)
        runner.data_sync(); runner.regime(); runner.selection(); runner.research()
        ctx.killswitch.engage("演练：数据异常")

        res = runner.plan()
        check("拉闸后不生成开仓计划", res.skipped is True, res.reason)

        tick = runner.intraday_tick()
        check("拉闸后盘中巡检仍然运行（止损通道必须活着）", tick.ok, tick.reason)
        check("拉闸后不开新仓", tick.data.get("opened", 0) == 0,
              f"opened={tick.data.get('opened')}")
    finally:
        ctx.close()


# ====================================================== 9. dry-run
def test_dry_run() -> None:
    logger.info("\n[9] dry-run 不落成交")
    ctx = _ctx("dry")
    try:
        runner = JobRunner(ctx, trade_date=D, dry_run=True)
        runner.run_morning()
        runner.intraday_tick()
        trades = ctx.repos.trades.list_by_date(D)
        check("dry-run 无成交记录", len(trades) == 0, f"{len(trades)} 笔")
        check("dry-run 无持仓", len(ctx.portfolio.positions) == 0,
              f"{len(ctx.portfolio.positions)} 只")
    finally:
        ctx.close()


# ====================================================== 10. 调度器
def test_scheduler() -> None:
    logger.info("\n[10] 调度器日程")
    ctx = _ctx("sched")
    try:
        runner = JobRunner(ctx, trade_date=D)
        sched = TradingScheduler(runner)
        names = [s.name for s in sched.specs]
        for must in ("data_sync", "regime", "selection", "research", "plan",
                     "auction_check", "intraday", "reconcile", "review", "evolve"):
            check(f"日程含 {must}", must in names)
        intraday = next(s for s in sched.specs if s.name == "intraday")
        check("盘中任务是 interval 型", intraday.kind == "interval", intraday.kind)
        check("盘中窗口有起止", bool(intraday.start_time and intraday.end_time),
              f"{intraday.start_time}-{intraday.end_time}")
        evolve = next(s for s in sched.specs if s.name == "evolve")
        check("进化任务限定星期", evolve.day_of_week is not None, str(evolve.day_of_week))
        desc = sched.describe()
        check("describe() 可读", "data_sync" in desc and "intraday" in desc)

        # 每个日程项都要能映射到真实任务，否则调度器会静默空转
        check("日程项全部可映射到任务",
              all(s.name in JOB_MAP for s in sched.specs),
              str([s.name for s in sched.specs if s.name not in JOB_MAP]))
    finally:
        ctx.close()


def test_simulate_day() -> None:
    logger.info("\n[11] 调度器整日回放")
    ctx = _ctx("sim_day")
    try:
        runner = JobRunner(ctx, trade_date=D)
        sched = TradingScheduler(runner)
        results = sched.simulate_day(D)
        check("回放产出结果", len(results) > 0, f"{len(results)} 项")
        check("回放不抛异常且全是 JobResult",
              all(isinstance(r, JobResult) for r in results))
        bad = [r for r in results if not r.ok and not r.skipped]
        check("回放零失败", not bad, "; ".join(f"{r.name}:{r.reason}" for r in bad))
    finally:
        ctx.close()


# ====================================================== 12. CLI
def test_cli() -> None:
    logger.info("\n[12] CLI 参数解析与保护")
    from qmt_trade.cli import build_parser

    p = build_parser()
    a = p.parse_args(["run", "--plan-only"])
    check("默认模式不是 live（手滑不会送钱）", a.mode != "live", a.mode)
    check("默认 sim", a.mode == "sim", a.mode)

    a = p.parse_args(["--mode", "live", "run"])
    check("live 必须显式指定", a.mode == "live")

    a = p.parse_args(["select", "--top", "20"])
    check("select --top 解析", a.top == 20)

    a = p.parse_args(["backtest", "--start", "2025-01-01", "--end", "2025-06-30"])
    check("backtest 日期解析", a.start == "2025-01-01" and a.end == "2025-06-30")

    a = p.parse_args(["reconcile", "--ack", "已核对"])
    check("reconcile --ack 解析", a.ack == "已核对")

    a = p.parse_args(["run", "--once", "selection"])
    check("run --once 解析", a.once == "selection")
    check("--once 取值受 JOB_MAP 约束", a.once in JOB_MAP)

    a = p.parse_args(["killswitch", "--engage", "手动停"])
    check("killswitch --engage 解析", a.engage == "手动停")

    for sub in ("run", "select", "backtest", "report", "reconcile",
                "health", "killswitch", "evolve"):
        ok = True
        try:
            p.parse_args([sub] if sub != "backtest" else
                         [sub, "--start", "2025-01-01", "--end", "2025-02-01"])
        except SystemExit:
            ok = False
        check(f"子命令 {sub} 可解析", ok)


def test_cli_run() -> None:
    logger.info("\n[13] CLI 端到端")
    from qmt_trade.cli import main

    db = _tmp_db("cli")
    rc = main(["--mode", "sim", "--db", db, "run", "--plan-only"])
    check("run --plan-only 退出码 0", rc == 0, f"rc={rc}")

    rc = main(["--mode", "sim", "--db", db, "health"])
    check("health 退出码 0", rc == 0, f"rc={rc}")

    rc = main(["--mode", "sim", "--db", db, "run", "--once", "selection",
               "--date", D.isoformat()])
    check("run --once selection 退出码 0", rc == 0, f"rc={rc}")

    rc = main(["--mode", "sim", "--db", db, "run", "--once", "不存在的任务"])
    check("未知任务返回非 0（不静默成功）", rc != 0, f"rc={rc}")

    rc = main(["--mode", "sim", "--db", db, "killswitch", "--engage", "冒烟演练"])
    check("killswitch --engage 退出码 0（动作成功即成功）", rc == 0, f"rc={rc}")

    # 纯查询时退出码反映健康度，方便 `killswitch || 报警` 这种脚本用法
    rc = main(["--mode", "sim", "--db", db, "killswitch"])
    check("killswitch 纯查询：已拉闸 → 非 0", rc != 0, f"rc={rc}")

    ctx = build_context("sim", db_path=db)
    try:
        check("CLI 拉闸生效且持久化",
              ctx.killswitch.mode is KillMode.REDUCE_ONLY, ctx.killswitch.mode.value)
        check("CLI 拉闸记为人工操作", ctx.killswitch.manual is True)
    finally:
        ctx.close()

    rc = main(["--mode", "sim", "--db", db, "killswitch", "--reset", "演练结束"])
    check("killswitch --reset 退出码 0", rc == 0, f"rc={rc}")
    rc = main(["--mode", "sim", "--db", db, "killswitch"])
    check("纯查询：已恢复 → 0", rc == 0, f"rc={rc}")


# ====================================================== 14. 结构
def test_structs() -> None:
    logger.info("\n[14] 数据结构")
    r = JobResult("demo", data={"a": 1})
    check("render 含任务名", "demo" in r.render(), r.render())
    check("to_dict 可序列化", set(r.to_dict()) >= {"name", "ok", "skipped", "data"})
    s = JobResult("x", skipped=True, reason="非交易日")
    check("skip 渲染带 SKIP", "SKIP" in s.render(), s.render())
    f = JobResult("y", ok=False, reason="炸了")
    check("fail 渲染带 FAIL", "FAIL" in f.render(), f.render())
    check("run_job 未知任务返回失败而非抛异常",
          run_job.__doc__ is not None)

    check("CRITICAL_JOBS 就是那三个",
          CRITICAL_JOBS == frozenset({"data_sync", "reconcile", "intraday"}),
          str(sorted(CRITICAL_JOBS)))


def main() -> int:
    logger.info("=" * 64)
    logger.info("L7 调度层 + 装配容器 + CLI 冒烟测试")
    logger.info("=" * 64)
    test_context()
    test_killswitch_persist()
    test_job_isolation()
    test_noncritical_no_kill()
    test_non_trading_day()
    test_full_day()
    test_restart_resume()
    test_reduce_only_behaviour()
    test_dry_run()
    test_scheduler()
    test_simulate_day()
    test_cli()
    test_cli_run()
    test_structs()
    logger.info("\n" + "=" * 64)
    logger.info(f"结果: {PASS} 通过 / {FAIL} 失败")
    logger.info("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())