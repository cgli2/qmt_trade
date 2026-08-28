"""命令行入口。

::

    python -m qmt_trade run --mode paper              # 起调度器，按日程自动跑
    python -m qmt_trade run --once selection          # 只跑一个任务
    python -m qmt_trade run --replay 2026-08-07       # 把某天整套流程跑一遍
    python -m qmt_trade select --top 20               # 看看今天选出什么
    python -m qmt_trade backtest --mode paper --start 2025-01-01 --end 2025-06-30
    python -m qmt_trade report --date 2026-08-07 --push
    python -m qmt_trade reconcile --ack "已核对券商流水，差异为手工卖出"
    python -m qmt_trade health
    python -m qmt_trade killswitch --status

约定：**所有会真实下单的命令默认 dry-run 之外还要显式 ``--mode live``**，
不给"手滑打错参数就把真钱送出去"留任何机会。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

from .app import ContextError, TradingContext, build_context
from .core.config import Settings
from .core.logging import setup_logging
from .core.strategies import is_standalone_strategy
from .risk.killswitch import KillMode

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"日期格式应为 YYYY-MM-DD，收到 {text!r}")


def _make_context(args) -> TradingContext:
    settings = Settings.load(args.config) if getattr(args, "config", None) else None
    return build_context(args.mode, settings=settings, db_path=getattr(args, "db", None))


# ==================================================================== run
def cmd_run(args) -> int:
    from .scheduler.jobs import JobRunner, run_job
    from .scheduler.runner import TradingScheduler

    with _make_context(args) as ctx:
        runner = JobRunner(ctx, trade_date=_parse_date(args.date), dry_run=args.dry_run)

        if args.once:
            res = run_job(runner, args.once)
            logger.info(res.render())
            return EXIT_OK if res.ok else EXIT_FAIL

        if args.replay:
            runner._forced_date = _parse_date(args.replay)   # noqa: SLF001
            results = runner.run_once_all()
            logger.info(runner.summary())
            return EXIT_OK if all(r.ok for r in results) else EXIT_FAIL

        sched = TradingScheduler(runner, ctx.settings)
        logger.info(sched.describe())
        if args.plan_only:
            return EXIT_OK
        logger.info(f"运行模式: {ctx.mode}" + ("（dry-run，不会真实下单）" if args.dry_run else "") + "  Ctrl-C 退出")
        sched.run_forever()
    return EXIT_OK


# ==================================================================== select
def cmd_select(args) -> int:
    with _make_context(args) as ctx:
        day = _parse_date(args.date) or date.today()
        cs = ctx.pipeline.run(day, top_n=args.top)
        logger.info(cs.report())
        if args.research and not cs.is_empty:
            from .scheduler.jobs import JobRunner
            runner = JobRunner(ctx, trade_date=day)
            runner.cache["candidates"] = cs
            res = runner.research()
            logger.info(res.render())
            for it in (runner.cache.get("intents") or []):
                logger.info(f"  {it.symbol} {it.action:<6} conf={it.confidence:.2f} " f"{it.conviction:<6} 止损={it.stop_loss_type}:{it.stop_loss_value} " f"{it.reasoning[:60]}")
        return EXIT_OK if not cs.is_empty else EXIT_FAIL


# ==================================================================== backtest
def _benchmark_return(hub, start: date, end: date) -> float | None:
    """同期沪深300收益（区间首末收盘比）。取数失败返回 None，绝不阻塞回测。"""
    try:
        if not getattr(hub, "providers", {}):
            return None
        idx = hub.get_index_bars("000300.SH", start, end, asof=end)
        if idx is None or idx.empty or "close" not in idx.columns:
            return None
        closes = idx["close"].dropna()
        if len(closes) < 2:
            return None
        return float(closes.iloc[-1] / closes.iloc[0] - 1.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("基准(沪深300)收益计算失败，跳过: %s", exc)
        return None


def cmd_backtest(args) -> int:
    from .backtest.engine import BacktestEngine

    start = _parse_date(args.start)
    end = _parse_date(args.end) or date.today()
    if start is None:
        logger.error("必须指定 --start")
        return EXIT_USAGE

    with _make_context(args) as ctx:
        # ---- 数据真实性护栏（软隔离铁律）：回测命令绝不接受 mock/假数据 ----
        # sim 模式（CLI 默认）使用 MockProvider：随机捏造虚拟代码（如 000370.SZ
        # 这类 A 股根本不存在的票）+ 纯随机游走行情，跑出的"收益/胜率"与真实市场
        # 毫无关系，却长得和真回测一模一样。故回测路径一律硬性拒绝 mock：
        # 检测到直接 exit，不跑、不报业绩。（MockProvider 仍保留给离线单测/冒烟。）
        provider_names = list(getattr(ctx.hub, "providers", {}).keys())
        is_mock = "mock" in provider_names
        if is_mock:
            logger.error("=" * 66)
            logger.error("✗ 回测已拒绝：检测到 MockProvider（虚拟标的 + 随机行情）。")
            logger.error("  回测必须在真实数据上进行，严禁用假数据得出任何业绩结论。")
            logger.error("  请加 --mode paper（需 qmt 或 akshare 且联网）后重试；")
            logger.error("  MockProvider 仅用于离线代码链路测试，不可用于回测。")
            logger.error("=" * 66)
            return EXIT_USAGE
        # ---- 独立策略（尾盘选股法等）：不走 SelectionPipeline/因子预设，路由到专属回测引擎 ----
        if is_standalone_strategy(args.strategy):
            return _cmd_backtest_standalone(args, ctx, start, end, is_mock, provider_names)
        brain = ctx.brain if args.llm else None
        engine = BacktestEngine(ctx.settings, ctx.hub,
                                initial_cash=args.cash, top_n=args.top,
                                max_holding_days=args.max_holding_days,
                                fixed_start=start - timedelta(days=args.warmup),
                                brain=brain, strategy=args.strategy)
        result = engine.run(start, end)
        # ---- 基准对比：拉沪深300同期收益，算超额（本项目核心痛点=跑赢自己跑输基准）----
        bench_ret = _benchmark_return(ctx.hub, start, end)
        total_ret = (result.metrics or {}).get("total_return")
        excess = (float(total_ret) - bench_ret) if (bench_ret is not None and total_ret is not None) else None
        # 在指标里打标，任何下游/截图都不会再把 mock 误认为真实行情
        if result.metrics is not None:
            result.metrics["data_mode"] = ("sim_mock(虚拟数据)"
                                           if is_mock else "real(" + ",".join(provider_names) + ")")
            result.metrics["benchmark_return"] = bench_ret
            result.metrics["excess_return"] = excess
        if not result.metrics:
            logger.info("回测未产出指标：" + "; ".join(result.details[:3]))
            return EXIT_FAIL
        logger.info(f"回测区间 {start} ~ {end}   初始资金 {args.cash:,.0f}")
        logger.info(f"  {'数据性质':<22} {result.metrics.get('data_mode', 'unknown')}")
        logger.info("-" * 52)
        m = result.metrics
        # F4 口径纪律：短窗口（<120 交易日）cagr/sharpe 置 None，报告里显示 n/a，
        # 不再出现"47 天年化 44.6%"这种数学正确、经济无意义的数字。
        if not m.get("annualized_valid", False):
            logger.info("  ⚠ 窗口仅 %s 个交易日（<120）：CAGR/Sharpe 不年化（无统计意义）",
                        m.get("n_days"))
        if not m.get("win_rate_sample_valid", False):
            logger.info("  ⚠ 已平仓 %s 笔（<20）：win_rate 仅供参考，不作结论",
                        m.get("n_sells"))
        for k, v in m.items():
            if k in ("annualized_valid", "win_rate_sample_valid",
                     "benchmark_return", "excess_return"):
                continue  # 口径标记/基准已在下方单独展示
            if isinstance(v, (int, float)):
                logger.info(f"  {k:<22} {v:>12.4f}")
            elif v is None:
                logger.info(f"  {k:<22} {'n/a（样本不足）':>12}")
            else:
                logger.info(f"  {k:<22} {v}")
        # ---- 基准对比展示 ----
        if bench_ret is not None:
            logger.info(f"  {'基准(沪深300)':<22} {bench_ret:>12.4f}")
            ex_warn = "  ⚠ 策略跑输基准" if (excess is not None and excess < 0) else ""
            logger.info(f"  {'超额收益(excess)':<22} {(excess or 0):>12.4f}{ex_warn}")
        else:
            logger.info(f"  {'基准(沪深300)':<22} {'n/a（取数失败）':>12}")
        logger.info(f"  {'成交笔数':<22} {len(result.trades):>12}")
        logger.info(f"  {'平仓笔数':<22} {len(result.closed_trades):>12}")
        # ---- 逐笔平仓明细（诊断 0% 胜率根因）----
        # closed_trades 已含每笔平仓的 reason（STOP_LOSS/TIME_STOP/TP_PARTIAL/
        # REGIME_CUT/TRAILING/INVALIDATE）与盈亏，打印出来即可直接看出
        # "盈利单为何从未出现"——是止损吞掉、时间止损、还是 Regime 砍仓。
        if result.closed_trades:
            logger.info("  " + "-" * 50)
            logger.info("  平仓明细（盈亏 = 平仓价相对成本）：")
            n_win = 0
            for t in result.closed_trades:
                pnl = float(t.get("pnl", 0.0) or 0.0)
                entry = float(t.get("entry_price", 0.0) or 0.0)
                shares = float(t.get("shares", 0) or 0)
                basis = entry * shares if entry > 0 and shares > 0 else 0.0
                pct = (pnl / basis) if basis > 0 else 0.0
                tag = "盈" if pnl > 0 else "亏"
                if pnl > 0:
                    n_win += 1
                logger.info(
                    f"    [{tag}] {str(t.get('symbol', '')):<8} "
                    f"{str(t.get('reason', '')):<12} 持有{int(t.get('holding_days', 0)):>2}d "
                    f"盈亏 {pnl:>+10.1f} ({pct:+.2%})")
            logger.info(f"  其中盈利 {n_win}/{len(result.closed_trades)}")
        # ---- 单票利润集中度（铁律：单票贡献>50% 视为特异性运气非稳健策略）----
        # 把同一标的多次平仓（如先止损后 trailing 再赚）按 symbol 汇总净盈亏，
        # 算"单票净贡献 / 组合总净盈亏"，>50% 直接标红，防止被一只票绑架还看不出。
        _sym_pnl: dict[str, float] = {}
        for _t in result.closed_trades:
            _s = str(_t.get("symbol", ""))
            _sym_pnl[_s] = _sym_pnl.get(_s, 0.0) + float(_t.get("pnl", 0) or 0)
        _total_net = sum(_sym_pnl.values())
        if _sym_pnl and _total_net != 0:
            _top_sym = max(_sym_pnl, key=lambda s: abs(_sym_pnl[s]))
            _top_pct = _sym_pnl[_top_sym] / _total_net * 100.0
            _warn = "  ⚠ 单票贡献>50%：特异性运气非稳健策略" if abs(_top_pct) > 50 else ""
            logger.info(f"  单票最大利润占比 {_top_sym:<8} {_top_pct:>7.1f}%{_warn}")
        # ---- 未平仓持仓浮盈（解开"组合盈利但 win_rate=0"的悖论）----
        # win_rate 只数已平仓的 realized_log；窗口末仍持有的盈利仓被完全排除，
        # 于是出现"组合 +6% 但胜率 0%"的假象。这里把浮盈仓一并计入"含浮盈组合胜率"。
        open_pos = getattr(result, "open_positions", None)
        if open_pos:
            n_open = len(open_pos)
            n_open_win = 0
            logger.info("  " + "-" * 50)
            logger.info("  未平仓持仓浮盈（期末市值相对成本）：")
            for p in open_pos:
                avg = float(p.get("avg_cost") or 0.0)
                last = float(p.get("last_price") or avg)
                sh = float(p.get("shares", 0) or 0)
                basis = avg * sh
                mark = (last - avg) * sh
                pct = (mark / basis) if basis > 0 else 0.0
                if mark > 0:
                    n_open_win += 1
                tag = "盈" if mark > 0 else "亏"
                logger.info(
                    f"    [{tag}] {str(p.get('symbol', '')):<8} "
                    f"自{str(p.get('opened_at', '')):<10} 浮盈 {mark:>+10.1f} ({pct:+.2%})")
            n_win_closed = sum(1 for t in result.closed_trades
                               if (float(t.get("pnl", 0) or 0)) > 0)
            n_closed = len(result.closed_trades)
            n_total = n_closed + n_open
            comb = ((n_win_closed + n_open_win) / n_total) if n_total else 0.0
            logger.info(f"  含浮盈组合胜率 = {comb:.1%}  "
                        f"（已平仓盈利 {n_win_closed}/{n_closed}，未平仓盈利 {n_open_win}/{n_open}）")
            logger.info("  ⚠ win_rate 指标仅统计已平仓，未含上方浮盈仓；单独看会低估真实胜率")
        return EXIT_OK


# ==================================================================== 独立策略回测
def _cmd_backtest_standalone(args, ctx, start: date, end: date,
                             is_mock: bool, provider_names: list[str]) -> int:
    """独立策略（尾盘/打板/二板/低吸/趋势）的回测：统一注册表构造，同数据真实性护栏。"""
    from .core.strategies import build_standalone_backtester

    bt = build_standalone_backtester(args.strategy, ctx.settings, ctx.hub,
                                     initial_cash=args.cash)
    _cfg = getattr(bt, "cfg", None) or getattr(bt, "config", None)
    if args.strategy == "tail_pick" and _cfg is not None and not _cfg.enabled and ctx.mode == "paper":
        logger.info("提示：strategies.tail_pick.enabled=false，模拟盘下仅做机制验证"
                    "（数字【非真实业绩】）。实盘前请在 paper/live 开启并接入分钟线。")
    result = bt.run(start, end)
    if result.metrics is None:
        logger.info("回测未产出指标：" + "; ".join(result.details[:3]))
        return EXIT_FAIL
    result.metrics["data_mode"] = ("sim_mock(虚拟数据)"
                                    if is_mock else "real(" + ",".join(provider_names) + ")")
    _minute = getattr(result, "minute_available", None)
    result.metrics["minute_available"] = _minute
    if args.strategy == "tail_pick" and _minute is False:
        logger.warning("⚠ 无分钟线源：分钟依赖规则⑥⑦⑧按 best-effort 放行，"
                       "本回测【仅验证机制，非真实业绩】。")

    m = result.metrics
    bm = _benchmark_return(ctx.hub, start, end)
    logger.info(f"独立策略[{args.strategy}]回测 interval {start} ~ {end}   初始资金 {args.cash:,.0f}")
    logger.info(f"  {'数据性质':<22} {m.get('data_mode', 'unknown')}")
    if _minute is not None:
        logger.info(f"  {'分钟线':<22} {'可用(精确撮合)' if _minute else '不可用(日线降级)'}")
    if bm is not None:
        logger.info(f"  {'同期沪深300':<22} {bm:>12.4f}")
        _total_ret = m.get("total_return")
        _excess = (float(_total_ret) - bm) if _total_ret is not None else None
        logger.info(f"  {'空仓基准(现金)':<22} {0.0:>12.4f}")
        if _excess is not None:
            _ex_warn = "  ⚠ 跑输基准" if _excess < 0 else ""
            logger.info(f"  {'超额收益(策略-沪深300)':<22} {_excess:>12.4f}{_ex_warn}")
    logger.info("-" * 52)
    if not m.get("annualized_valid", False):
        logger.info("  ⚠ 窗口仅 %s 个交易日（<120）：CAGR/Sharpe 不年化（无统计意义）",
                    m.get("n_days"))
    if not m.get("win_rate_sample_valid", False):
        logger.info("  ⚠ 已平仓 %s 笔（<20）：win_rate 仅供参考，不作结论",
                    m.get("n_sells"))
    for k, v in m.items():
        if k in ("annualized_valid", "win_rate_sample_valid", "data_mode", "minute_available"):
            continue
        if isinstance(v, (int, float)):
            logger.info(f"  {k:<22} {v:>12.4f}")
        elif v is None:
            logger.info(f"  {k:<22} {'n/a（样本不足）':>12}")
        else:
            logger.info(f"  {k:<22} {v}")
    logger.info(f"  {'成交笔数':<22} {len(result.trades):>12}")
    logger.info(f"  {'平仓笔数':<22} {len(result.closed_trades):>12}")
    if result.closed_trades:
        logger.info("  " + "-" * 50)
        logger.info("  平仓明细（盈亏 = 平仓价相对成本）：")
        n_win = 0
        for t in result.closed_trades:
            pnl = float(t.get("pnl", 0.0) or 0.0)
            entry = float(t.get("entry_price", 0.0) or 0.0)
            shares = float(t.get("shares", 0) or 0)
            basis = entry * shares if entry > 0 and shares > 0 else 0.0
            pct = (pnl / basis) if basis > 0 else 0.0
            if pnl > 0:
                n_win += 1
            logger.info(
                f"    [{'盈' if pnl > 0 else '亏'}] {str(t.get('symbol', '')):<8} "
                f"{str(t.get('reason', '')):<10} 持有{int(t.get('holding_days', 0)):>2}d "
                f"盈亏 {pnl:>+10.1f} ({pct:+.2%})")
        logger.info(f"  已平仓胜率 = {n_win}/{len(result.closed_trades)}")

    # ---- P0 成本归因（2026-08-15）：毛/净拆分 + 换手 + 往返成本率 ----
    # 净盈亏 = 信号毛盈亏 − 滑点 − 佣金/印花/过户。滑点在 SimGateway 里已计入
    # 成交价（买入 +0.2%、卖出 −0.2%），报告层面必须拆开，否则成本拖累永远隐形。
    ca = getattr(result, "cost_attribution", None) or {}
    if ca:
        _init = float(args.cash) or 1.0
        logger.info("  " + "-" * 50)
        logger.info("  成本归因（净盈亏 = 信号毛盈亏 − 滑点 − 佣金/印花/过户）：")
        for _label, _key in (("信号毛盈亏(零成本口径)", "gross_pnl"),
                             ("成本拖累", "cost_drag"),
                             ("  其中 滑点", "slippage"),
                             ("  其中 佣金/印花/过户", "explicit_fees"),
                             ("净盈亏(已含成本)", "net_pnl")):
            _v = float(ca.get(_key, 0.0) or 0.0)
            logger.info(f"    {_label:<20} {_v:>+14,.0f}  ({_v / _init:+.2%} 期初资金)")
        logger.info(f"    单边年换手(期初资金倍数)   {float(ca.get('single_side_turnover', 0.0)):>9.1f}×")
        logger.info(f"    往返成本率(每完成一次买卖) {float(ca.get('roundtrip_cost_rate', 0.0)):>8.2%}")
        logger.info(f"    真实笔数(合并TP/TRAIL拆笔) {int(ca.get('n_round_trips', 0))} "
                    f"（离场记录 {int(ca.get('n_sells', 0))}）")

    # ---- P0 离场信号归因（2026-08-15）：GAPCUT 等信号 × 笔数 × 胜率 × 收益 ----
    ga = getattr(result, "gap_attribution", None) or []
    if ga:
        logger.info("  " + "-" * 50)
        logger.info("  离场信号归因（信号 × 笔数 × 胜率 × 笔均持有收益 × 盈亏）：")
        for _r in ga:
            _is_all = str(_r["reason"]) == "ALL"
            _label = "全部" if _is_all else str(_r["reason"])
            _tail = "" if _is_all else f" (占离场 {float(_r['pct_of_exits']):.0%})"
            logger.info(
                f"    {_label:<14} n={int(_r['n']):>5} "
                f"胜率={float(_r['win_rate']):>6.1%} "
                f"笔均收益={float(_r['avg_ret']):>+7.2%} "
                f"笔均={float(_r['avg_pnl']):>+9,.0f} "
                f"合计={float(_r['total_pnl']):>+12,.0f}{_tail}")
    return EXIT_OK


# ==================================================================== tailpick 子命令
def cmd_tailpick(args) -> int:
    from .strategies.tail_pick import TailPickStrategy, TailPickConfig

    if args.action == "select":
        with _make_context(args) as ctx:
            day = _parse_date(args.date) or date.today()
            strat = TailPickStrategy(ctx.settings, ctx.hub)
            # sim 无分钟线 → best-effort；paper/live 尝试严格
            minute = True
            picks = strat.select(day, minute_available=minute)
            if not picks:
                logger.info(f"{day} 尾盘选股：无标的通过 8 层筛选")
                return EXIT_OK
            logger.info(f"{day} 尾盘选股通过 {len(picks)} 只（minute_verified={picks[0].minute_verified}）：")
            for p in picks:
                logger.info(f"  {p.symbol:<10} 入场{p.entry_price:>8.2f}  "
                            f"涨{p.pct_change:+.1%} 量比{p.volume_ratio:.1f} "
                            f"换手{p.turnover_rate:.1%} 流通{p.float_market_cap/1e8:.0f}亿  "
                            f"{'; '.join(p.reasons)}")
            return EXIT_OK

    if args.action == "backtest":
        with _make_context(args) as ctx:
            start = _parse_date(args.start)
            end = _parse_date(args.end) or date.today()
            if start is None:
                logger.error("backtest 必须指定 --start")
                return EXIT_USAGE
            provider_names = list(getattr(ctx.hub, "providers", {}).keys())
            is_mock = "mock" in provider_names
            if is_mock:
                logger.error("=" * 66)
                logger.error("✗ tailpick 回测已拒绝：检测到 MockProvider（虚拟标的 + 随机行情）。")
                logger.error("  回测必须在真实数据上进行，严禁用假数据得出任何业绩结论。")
                logger.error("  请加 --mode paper（需 qmt 或 akshare 且联网）后重试。")
                logger.error("=" * 66)
                return EXIT_USAGE
            return _cmd_backtest_standalone(args, ctx, start, end, is_mock, provider_names)

    if args.action == "validate":
        # 用内置合成数据自检 8 层筛选 + 一夜持股买卖/成本/止损机制（不依赖任何真实源）
        try:
            from tests.test_tail_pick import run_selfcheck
        except Exception as exc:  # noqa: BLE001
            logger.error("未找到自检模块 tests.test_tail_pick（请先运行 pytest）: %s", exc)
            return EXIT_FAIL
        rep = run_selfcheck()
        ok = rep.get("ok", False)
        logger.info("尾盘选股法机制自检：%s", "通过 ✅" if ok else "失败 ❌")
        for line in rep.get("lines", []):
            logger.info("  " + line)
        return EXIT_OK if ok else EXIT_FAIL

    logger.error("未知 action：%s", args.action)
    return EXIT_USAGE


# ==================================================================== report
def cmd_report(args) -> int:
    with _make_context(args) as ctx:
        day = _parse_date(args.date) or date.today()
        rep = (ctx.reporter.weekly(day) if args.weekly
               else ctx.reporter.daily(day))
        logger.info(rep.to_markdown())
        if args.save:
            logger.info(f"\n已写入 {ctx.reporter.save(rep)}")
        if args.push:
            ok = ctx.reporter.push(rep)
            logger.info("推送成功" if ok else "推送失败或未配置通道")
        return EXIT_OK


# ==================================================================== reconcile
def cmd_reconcile(args) -> int:
    with _make_context(args) as ctx:
        day = _parse_date(args.date) or date.today()

        if args.ack:
            ok = ctx.reconciler.acknowledge(day, operator=args.operator, note=args.ack)
            logger.info(f"人工确认{'成功' if ok else '失败'}：{day} — {args.ack}")
            logger.info(f"当前 KillSwitch: {ctx.killswitch.mode.value}")
            return EXIT_OK if ok else EXIT_FAIL

        broker = ctx.gateway
        if not hasattr(broker, "query_positions"):
            logger.error(f"{ctx.mode} 模式没有券商可对账，请用 --mode live")
            return EXIT_USAGE
        res = ctx.reconciler.run(day, broker)
        logger.info(res.render())
        return EXIT_OK if res.passed else EXIT_FAIL


# ==================================================================== health
def cmd_health(args) -> int:
    from .scheduler.runner import TradingScheduler, next_run_at
    from .scheduler.jobs import JobRunner

    with _make_context(args) as ctx:
        rep = ctx.monitor.check(notify=args.notify)
        logger.info(rep.render())

        ks = ctx.killswitch
        logger.info(f"\nKillSwitch: {ks.mode.value}" + (f"  原因: {ks.reason}" if ks.reason else ""))

        rows = []
        for name in ("data_sync", "selection", "research", "intraday",
                     "reconcile", "review"):
            last = ctx.repos.system.get(f"job:{name}:last_run") or "-"
            status = ctx.repos.system.get(f"job:{name}:last_status") or "-"
            rows.append(f"  {name:<14} {status:<5} {last}")
        logger.info("\n最近任务执行：")
        logger.info("\n".join(rows))

        if args.schedule:
            sched = TradingScheduler(JobRunner(ctx), ctx.settings)
            now = datetime.now()
            logger.info("\n" + sched.describe())
            logger.info("\n下次触发：")
            for spec in sched.specs:
                nxt = next_run_at(spec, now)
                logger.info(f"  {spec.name:<14} {nxt:%Y-%m-%d %H:%M}" if nxt else f"  {spec.name:<14} -")
        return EXIT_OK if rep.healthy else EXIT_FAIL


# ==================================================================== killswitch
def cmd_killswitch(args) -> int:
    with _make_context(args) as ctx:
        ks = ctx.killswitch
        # 人从命令行操作的一律记为「人工」，事后复盘时要能和系统自动拉闸区分开
        changed = True
        if args.engage:
            ks.engage(args.engage, manual=True)
        elif args.flatten:
            ks.flatten(args.flatten, manual=True)
        elif args.reset:
            ks.reset(args.reset)
        else:
            changed = False
        logger.info(f"KillSwitch: {ks.mode.value}")
        if ks.reason:
            logger.info(f"原因: {ks.reason}")
        if ks.triggered_at:
            logger.info(f"时间: {ks.triggered_at:%Y-%m-%d %H:%M:%S}  " f"{'人工' if ks.manual else '自动'}")
        # 显式改档：只要动作做成了就是成功。
        # 纯查询：用退出码反映系统健康度，方便 `killswitch || 报警` 这种脚本用法。
        if changed:
            return EXIT_OK
        return EXIT_OK if ks.mode is KillMode.NORMAL else EXIT_FAIL


# ==================================================================== evolve
def cmd_evolve(args) -> int:
    from .scheduler.jobs import JobRunner

    with _make_context(args) as ctx:
        runner = JobRunner(ctx, trade_date=_parse_date(args.date))
        res = runner.evolve()
        logger.info(res.render())
        if res.data.get("weights"):
            logger.info("策略权重：" + ", ".join( f"{k}={v:.0%}" for k, v in sorted(res.data["weights"].items(), key=lambda kv: -kv[1])))
        return EXIT_OK if res.ok else EXIT_FAIL


# ==================================================================== parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qmt_trade", description="LLM 驱动的 A 股自动交易系统")
    p.add_argument("--mode", choices=("paper", "live"), default="paper",
                   help="运行模式：paper=真数据模拟撮合 / live=实盘")
    p.add_argument("--config", help="配置文件路径，默认 config/settings.yaml")
    p.add_argument("--db", help="SQLite 路径，默认 data/trade.db（live 模式默认 data/trade_live.db）")
    p.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    sub = p.add_subparsers(dest="command", required=True)

    # run
    r = sub.add_parser("run", help="启动调度器或执行单个任务")
    r.add_argument("--once", help="只执行一个任务（data_sync/selection/...）")
    r.add_argument("--replay", help="按指定日期把全流程跑一遍 YYYY-MM-DD")
    r.add_argument("--date", help="强制交易日 YYYY-MM-DD（配合 --once）")
    r.add_argument("--dry-run", action="store_true", help="只演算不下单")
    r.add_argument("--plan-only", action="store_true", help="只打印日程表后退出")
    r.set_defaults(func=cmd_run)

    # select
    s = sub.add_parser("select", help="跑一次选股")
    s.add_argument("--date", help="决策日 YYYY-MM-DD")
    s.add_argument("--top", type=int, default=None, help="保留前 N 只")
    s.add_argument("--research", action="store_true", help="顺带跑 L2 研判")
    s.set_defaults(func=cmd_select)

    # backtest
    b = sub.add_parser("backtest", help="历史回测")
    b.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    b.add_argument("--end", help="结束日期，默认今天")
    b.add_argument("--cash", type=float, default=1_000_000.0, help="初始资金")
    b.add_argument("--top", type=int, default=10, help="每日最多持仓数")
    b.add_argument("--warmup", type=int, default=250,
                   help="因子预热天数（固定历史起点，保证可复现）")
    b.add_argument("--llm", action="store_true", help="启用 L2 多智能体（慢且花钱）")
    b.add_argument("--strategy", default=None,
                   help="策略预设 id（balanced/momentum_breakout/value_quality/"
                        "moneyflow_resonance/low_vol_defensive）或不走因子体系的独立策略"
                        "（tail_pick=尾盘选股法 / limit_up=打板 / second_board=二板龙头 / "
                        "dip_buy=尾盘低吸 / trend_buy=趋势买点 / etf_t0=ETF T+0 日内回转），不传=原始默认行为")
    b.add_argument("--max-holding-days", type=int, default=20, help="时间止损天数")
    b.set_defaults(func=cmd_backtest)

    # report
    rp = sub.add_parser("report", help="生成日报/周报")
    rp.add_argument("--date", help="报告日期 YYYY-MM-DD")
    rp.add_argument("--weekly", action="store_true", help="出周报")
    rp.add_argument("--save", action="store_true", help="写入 reports 目录")
    rp.add_argument("--push", action="store_true", help="推送到通知渠道")
    rp.set_defaults(func=cmd_report)

    # reconcile
    rc = sub.add_parser("reconcile", help="盘后对账 / 人工确认")
    rc.add_argument("--date", help="对账日期 YYYY-MM-DD")
    rc.add_argument("--ack", help="人工确认差异并解除限制，需写明理由")
    rc.add_argument("--operator", default="cli", help="确认人")
    rc.set_defaults(func=cmd_reconcile)

    # health
    h = sub.add_parser("health", help="系统体检")
    h.add_argument("--notify", action="store_true", help="体检结果推送告警")
    h.add_argument("--schedule", action="store_true", help="附带日程表与下次触发时刻")
    h.set_defaults(func=cmd_health)

    # killswitch
    k = sub.add_parser("killswitch", help="查看/操作总开关")
    k.add_argument("--status", action="store_true", help="仅查看当前档位（默认行为）")
    k.add_argument("--engage", metavar="REASON", help="降级为 REDUCE_ONLY")
    k.add_argument("--flatten", metavar="REASON", help="升级为 FLATTEN（全部平仓）")
    k.add_argument("--reset", nargs="?", const="人工恢复", metavar="REASON",
                   help="恢复 NORMAL")
    k.set_defaults(func=cmd_killswitch)

    # evolve
    e = sub.add_parser("evolve", help="策略池调权 + 周报")
    e.add_argument("--date", help="基准日期 YYYY-MM-DD")
    e.set_defaults(func=cmd_evolve)

    """已废弃：尾盘策略改由统一 strategy 接口管理。
    # tailpick（独立策略：尾盘选股法 / 一夜持股法）
    tp = sub.add_parser("tailpick", help="尾盘选股法独立策略（select/backtest/validate）")
    tp.add_argument("action", choices=("select", "backtest", "validate"),
                    help="select=看某日选股；backtest=区间回测；validate=合成数据机制自检")
    tp.add_argument("--date", help="选股决策日 YYYY-MM-DD（select 用）")
    tp.add_argument("--start", help="回测开始 YYYY-MM-DD（backtest 用）")
    tp.add_argument("--end", help="回测结束 YYYY-MM-DD（backtest 用）")
    tp.add_argument("--cash", type=float, default=1_000_000.0, help="初始资金")
    tp.set_defaults(func=cmd_tailpick)

    """
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        setup_logging(level=args.log_level) if args.log_level else setup_logging()
    except Exception:                                # noqa: BLE001 - 日志起不来不该挡住命令
        logging.basicConfig(level=args.log_level or "INFO")

    try:
        return int(args.func(args) or EXIT_OK)
    except ContextError as exc:
        logger.error(f"装配失败：{exc}")
        return EXIT_FAIL
    except KeyboardInterrupt:
        logger.info("\n已中断")
        return EXIT_OK
    except Exception as exc:                         # noqa: BLE001
        logger.exception("命令执行失败")
        logger.error(f"执行失败：{type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":                           # pragma: no cover
    raise SystemExit(main())