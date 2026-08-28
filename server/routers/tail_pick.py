"""尾盘选股法（一夜持股法）独立控制面板。

本策略完全独立于现有多因子选股/Regime/风控体系（选股层自立 TailPickScreener，
执行走同一套 SimGateway/CostModel 撮合口径）。本路由只读/写：

- ``strategies.tail_pick`` 配置段（开关 + 8 层筛选阈值 + 交易纪律）
- ``scheduler.jobs.tail_pick_select / tail_pick_exit`` 调度时刻

不触碰任何现有策略的键。手动触发复用现有 ``POST /scheduler/run``
（name=tail_pick_select / tail_pick_exit），不在本路由重复实现。
"""

from __future__ import annotations

import json
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, StrictBool

import server.context as ctx
from server.context import load_settings_editor, save_settings

router = APIRouter(prefix="/tailpick", tags=["tailpick"])

_JOB_NAMES = ("tail_pick_select", "tail_pick_exit")


def _ctx(mode: str = Query("paper")):
    return ctx.make_ctx(mode)


def _json_or(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:                                       # noqa: BLE001
        return default


# ==================================================================== 状态聚合
@router.get("/status")
def status(mode: str = Query("paper")):
    """聚合面板一次要的全部数据：配置、调度、执行留痕、KillSwitch、
    今日候选、隔夜持仓、策略订单与历史表现（胜率/累计盈亏）。"""
    c = _ctx(mode)
    s = load_settings_editor()
    cfg = s.section("strategies.tail_pick") or {}

    # ---- 调度：下次执行时间（复用 overview.scheduler_jobs 的组装方式）----
    from qmt_trade.scheduler.jobs import JobRunner
    from qmt_trade.scheduler.runner import TradingScheduler, next_run_at

    sched = TradingScheduler(JobRunner(c), c.settings)
    spec_map = {sp.name: sp for sp in sched.specs if sp.name in _JOB_NAMES}
    now = datetime.now()
    schedule = []
    for name in _JOB_NAMES:
        sp = spec_map.get(name)
        nxt = next_run_at(sp, now) if sp else None
        schedule.append({
            "name": name,
            "time_label": sp.time_label if sp else None,
            "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
        })

    # ---- 最近执行留痕（JobRunner._record 写在对应模式账本的 system_state）----
    jobs = {}
    for name in _JOB_NAMES:
        jobs[name] = {
            "last_run": c.repos.system.get(f"job:{name}:last_run"),
            "last_status": c.repos.system.get(f"job:{name}:last_status"),
            "last_error": c.repos.system.get(f"job:{name}:last_error"),
        }

    # ---- KillSwitch 协调：非 NORMAL 时只出不进（买入路径在 job 内已检查）----
    ks = c.killswitch.to_dict()
    ks["allow_open"] = bool(getattr(c.killswitch, "allow_open", False))

    # ---- 候选 / 当日买入（跨模式共享账本）----
    candidates = _json_or(c.shared_repos.system.get("selection:tail_pick:latest"), [])
    today = date.today()
    bought = {
        "today": _json_or(
            c.shared_repos.system.get(f"tail_pick:bought:{today.isoformat()}"), []),
    }
    try:
        from qmt_trade.core.clock import TradingCalendar
        prev = TradingCalendar().prev_trading_day(today)
    except Exception:                                       # noqa: BLE001
        prev = None
    if prev is not None:
        bought["yesterday"] = _json_or(
            c.shared_repos.system.get(f"tail_pick:bought:{prev.isoformat()}"), [])

    # ---- 策略订单与历史表现：orders.signal LIKE 'TAIL%'；
    #      trades 表无 signal 列，经 order_id 关联后按 realized_pnl 统计胜率 ----
    orders = [o for o in c.repos.orders.list_recent(500)
              if str(o.get("signal") or "").startswith("TAIL")]
    tp_ids = {o["id"] for o in orders}
    sells = [t for t in c.repos.trades.list_all()
             if t.get("order_id") in tp_ids and t.get("side") == "SELL"
             and t.get("realized_pnl") is not None]
    wins = [t for t in sells if (t["realized_pnl"] or 0) > 0]
    perf = {
        "n_orders": len(orders),
        "n_roundtrips": len(sells),
        "wins": len(wins),
        "losses": len(sells) - len(wins),
        "win_rate": (len(wins) / len(sells)) if sells else None,
        "total_pnl": sum(t["realized_pnl"] or 0 for t in sells),
    }

    # ---- 当前隔夜持仓：plan_id 前缀或当日/昨日买入名单命中 ----
    tp_syms = set(bought["today"]) | set(bought.get("yesterday", []))
    positions = [p for p in c.repos.positions.list_all()
                 if str(p.get("plan_id") or "").startswith("tp_sel_")
                 or p.get("symbol") in tp_syms]

    return {
        "mode": c.mode,
        "config": cfg,
        "schedule": schedule,
        "jobs": jobs,
        "killswitch": ks,
        "candidates": candidates,
        "bought": bought,
        "positions": positions,
        "orders": orders[:100],
        "perf": perf,
    }


# ==================================================================== 配置写入
class TailPickConfigIn(BaseModel):
    """全部字段可选，只写传入项。命名与 settings.yaml::strategies.tail_pick 一致。"""
    enabled: StrictBool | None = None
    select_time: str | None = None
    entry_time: str | None = None
    exit_window_start: str | None = None
    exit_window_end: str | None = None
    min_pct_change: float | None = None
    max_pct_change: float | None = None
    # V4.0 熊市反击版：弱市买跌带 + 缩量过滤 + 硬止损 + 弱市侦察兵仓位
    weak_min_pct_change: float | None = None
    weak_max_pct_change: float | None = None
    min_volume_ratio: float | None = None
    min_turnover_rate: float | None = None
    max_turnover_rate: float | None = None
    min_float_market_cap: float | None = None
    max_float_market_cap: float | None = None
    volume_ladder_ratio: float | None = None
    volume_ladder_segments: int | None = None
    volume_ladder_seg_tolerance: float | None = None
    shrink_vol_max_ratio: float | None = None
    vol_spike_exclude_ratio: float | None = None
    min_intraday_outperf_vs_index: float | None = None
    chip_vwap_tolerance_pct: float | None = None
    overnight_stop_pct: float | None = None
    max_positions: int | None = None
    position_fraction: float | None = None
    cash_usage_ratio: float | None = None
    universe_top_n: int | None = None
    require_minute_bars: bool | None = None
    # 离场增强 + 大市温度计（2026-08-13）
    gap_protect_enabled: bool | None = None
    gap_buffer_enabled: bool | None = None
    gap_buffer_pct: float | None = None
    breakeven_trigger_pct: float | None = None
    take_profit_pct: float | None = None
    hard_stop_pct: float | None = None
    vwap_exit_enabled: bool | None = None
    market_filter_enabled: bool | None = None
    market_ma_days: int | None = None
    market_breadth_required: bool | None = None
    weak_market_adv_min: int | None = None
    weak_market_position_ratio: float | None = None
    breadth_block_below: int | None = None
    exclude_st: bool | None = None
    min_list_days: int | None = None
    exclude_suspended: bool | None = None
    exclude_limit_locked: bool | None = None
    allowed_boards: list[str] | None = None
    # 时间不对称离场 + 板块效应（2026-08-13 四段式革命）
    gapup_threshold_pct: float | None = None
    gapdown_threshold_pct: float | None = None
    low_open_check_time: str | None = None
    open_momentum_enabled: bool | None = None
    open_momentum_vol_mult: float | None = None
    open_momentum_hold_until: str | None = None
    sector_enabled: bool | None = None
    sector_top_n: int | None = None
    sector_bottom_n: int | None = None
    sector_boost_mult: float | None = None


def _hm(text: str, field_name: str) -> str:
    """HH:MM 严格校验（同 overview._parse_hm_strict 语义）。"""
    parts = str(text).split(":")
    if len(parts) != 2:
        raise HTTPException(400, f"{field_name} 必须是 HH:MM 格式，收到 {text!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise HTTPException(400, f"{field_name} 必须是 HH:MM 格式，收到 {text!r}")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise HTTPException(400, f"{field_name} 超出范围（{h:02d}:{m:02d}）")
    return f"{h:02d}:{m:02d}"


def _num(body: TailPickConfigIn, key: str, lo: float, hi: float) -> float | None:
    v = getattr(body, key)
    if v is None:
        return None
    if not (lo <= float(v) <= hi):
        raise HTTPException(400, f"{key} 须在 [{lo}, {hi}] 之间，收到 {v}")
    return float(v)


_VALID_BOARDS = ("MAIN", "GEM", "STAR", "BSE")


@router.put("/config")
def update_config(body: TailPickConfigIn, request: Request):
    """写入策略参数：校验 → 落 settings.yaml → 选股/离场时刻联动调度 →
    热更新常驻调度器。只动 strategies.tail_pick 与 scheduler.jobs.tail_pick_*。"""
    s = load_settings_editor()
    cur = s.section("strategies.tail_pick") or {}
    applied: list[str] = []

    def _set(key: str, value):
        s.set(f"strategies.tail_pick.{key}", value)
        applied.append(key)

    # ---- 时刻（先收集，做跨字段校验 + 调度联动）----
    select_time = _hm(body.select_time, "选股时刻") if body.select_time else None
    entry_time = _hm(body.entry_time, "买入时刻") if body.entry_time else None
    exit_start = _hm(body.exit_window_start, "离场窗口起点") if body.exit_window_start else None
    exit_end = _hm(body.exit_window_end, "离场窗口终点") if body.exit_window_end else None
    eff_select = select_time or str(cur.get("select_time", "14:30"))
    eff_exit_start = exit_start or str(cur.get("exit_window_start", "09:30"))
    eff_exit_end = exit_end or str(cur.get("exit_window_end", "10:00"))
    if select_time and select_time < "13:00":
        raise HTTPException(400, "尾盘选股时刻应不早于 13:00（策略定义：14:30 前后）")
    if eff_exit_start >= eff_exit_end:
        raise HTTPException(
            400, f"离场窗口非法：起点 {eff_exit_start} 须早于终点 {eff_exit_end}"
                 "（一夜持股纪律：开盘 30 分钟内离场）")

    # ---- 8 层筛选阈值（成对区间校验）----
    min_pct = _num(body, "min_pct_change", 0.0, 0.20)
    max_pct = _num(body, "max_pct_change", 0.0, 0.20)
    eff_min_pct = min_pct if min_pct is not None else float(cur.get("min_pct_change", 0.03))
    eff_max_pct = max_pct if max_pct is not None else float(cur.get("max_pct_change", 0.05))
    if eff_min_pct >= eff_max_pct:
        raise HTTPException(
            400, f"涨幅区间非法：下界 {eff_min_pct} 须小于上界 {eff_max_pct}")
    # V4.0 弱市买跌带（成对区间校验，下界可负）
    weak_min_pct = _num(body, "weak_min_pct_change", -0.10, 0.10)
    weak_max_pct = _num(body, "weak_max_pct_change", 0.0, 0.10)
    eff_weak_min = weak_min_pct if weak_min_pct is not None \
        else float(cur.get("weak_min_pct_change", -0.01))
    eff_weak_max = weak_max_pct if weak_max_pct is not None \
        else float(cur.get("weak_max_pct_change", 0.015))
    if eff_weak_min >= eff_weak_max:
        raise HTTPException(
            400, f"弱市涨幅区间非法：下界 {eff_weak_min} 须小于上界 {eff_weak_max}")
    min_tr = _num(body, "min_turnover_rate", 0.0, 1.0)
    max_tr = _num(body, "max_turnover_rate", 0.0, 1.0)
    eff_min_tr = min_tr if min_tr is not None else float(cur.get("min_turnover_rate", 0.05))
    eff_max_tr = max_tr if max_tr is not None else float(cur.get("max_turnover_rate", 0.10))
    if eff_min_tr >= eff_max_tr:
        raise HTTPException(
            400, f"换手率区间非法：下界 {eff_min_tr} 须小于上界 {eff_max_tr}")
    min_cap = _num(body, "min_float_market_cap", 0, 1e13)
    max_cap = _num(body, "max_float_market_cap", 0, 1e13)
    eff_min_cap = min_cap if min_cap is not None else float(cur.get("min_float_market_cap", 5e9))
    eff_max_cap = max_cap if max_cap is not None else float(cur.get("max_float_market_cap", 5e10))
    if eff_min_cap >= eff_max_cap:
        raise HTTPException(400, "流通市值区间非法：下界须小于上界")

    single_floats = {
        "min_volume_ratio": (0.0, 100.0),
        "volume_ladder_ratio": (1.0, 100.0),
        "volume_ladder_seg_tolerance": (0.5, 10.0),
        "min_intraday_outperf_vs_index": (-0.2, 0.2),
        "chip_vwap_tolerance_pct": (0.0, 0.1),
        "overnight_stop_pct": (0.001, 0.2),
        "position_fraction": (0.01, 1.0),
        "cash_usage_ratio": (0.1, 1.0),
        "breakeven_trigger_pct": (0.0, 0.05),
        "take_profit_pct": (0.001, 0.1),
        "gap_buffer_pct": (0.0, 0.02),
        "weak_market_position_ratio": (0.05, 1.0),
        # V4.0 熊市反击版：缩量过滤 / 放量剔除 / 硬止损
        "shrink_vol_max_ratio": (0.5, 5.0),
        "vol_spike_exclude_ratio": (1.0, 10.0),
        "hard_stop_pct": (0.001, 0.1),
        "gapup_threshold_pct": (0.0, 0.05),
        "gapdown_threshold_pct": (0.0, 0.05),
        "open_momentum_vol_mult": (1.0, 10.0),
        "sector_boost_mult": (1.0, 5.0),
    }
    values: dict[str, object] = {}
    for key, (lo, hi) in single_floats.items():
        v = _num(body, key, lo, hi)
        if v is not None:
            values[key] = v

    if body.max_positions is not None and not (1 <= body.max_positions <= 10):
        raise HTTPException(400, "max_positions 须在 1~10 之间")
    if body.volume_ladder_segments is not None and not (2 <= body.volume_ladder_segments <= 8):
        raise HTTPException(400, "volume_ladder_segments 须在 2~8 之间")
    if body.universe_top_n is not None and not (5 <= body.universe_top_n <= 500):
        raise HTTPException(400, "universe_top_n 须在 5~500 之间")
    if body.min_list_days is not None and not (0 <= body.min_list_days <= 3650):
        raise HTTPException(400, "min_list_days 须在 0~3650 之间")
    if body.market_ma_days is not None and not (5 <= body.market_ma_days <= 250):
        raise HTTPException(400, "market_ma_days 须在 5~250 之间")
    if body.weak_market_adv_min is not None and not (0 <= body.weak_market_adv_min <= 6000):
        raise HTTPException(400, "weak_market_adv_min 须在 0~6000 之间")
    if body.breadth_block_below is not None and not (0 <= body.breadth_block_below <= 6000):
        raise HTTPException(400, "breadth_block_below 须在 0~6000 之间")
    if body.sector_top_n is not None and not (0 <= body.sector_top_n <= 50):
        raise HTTPException(400, "sector_top_n 须在 0~50 之间")
    if body.sector_bottom_n is not None and not (0 <= body.sector_bottom_n <= 300):
        raise HTTPException(400, "sector_bottom_n 须在 0~300 之间")
    low_open_check = _hm(body.low_open_check_time, "低开砍仓时刻") if body.low_open_check_time else None
    if low_open_check and not ("09:31" <= low_open_check <= "11:30"):
        raise HTTPException(400, f"low_open_check_time 须在 09:31~11:30 之间，收到 {low_open_check}")
    momentum_hold = _hm(body.open_momentum_hold_until, "动量持有截止时刻") \
        if body.open_momentum_hold_until else None
    if momentum_hold and not ("09:36" <= momentum_hold <= "14:55"):
        raise HTTPException(400, f"open_momentum_hold_until 须在 09:36~14:55 之间，收到 {momentum_hold}")
    if body.allowed_boards is not None:
        boards = [str(b).upper() for b in body.allowed_boards]
        bad = [b for b in boards if b not in _VALID_BOARDS]
        if bad:
            raise HTTPException(
                400, f"allowed_boards 含未知板块 {bad}，可选：{_VALID_BOARDS}")
        if not boards:
            raise HTTPException(400, "allowed_boards 不能为空")

    # ---- 落盘 ----
    if body.enabled is not None:
        _set("enabled", bool(body.enabled))
    if select_time:
        _set("select_time", select_time)
    if entry_time:
        _set("entry_time", entry_time)
    if exit_start:
        _set("exit_window_start", exit_start)
    if exit_end:
        _set("exit_window_end", exit_end)
    for key, v in (("min_pct_change", min_pct), ("max_pct_change", max_pct),
                   ("weak_min_pct_change", weak_min_pct), ("weak_max_pct_change", weak_max_pct),
                   ("min_turnover_rate", min_tr), ("max_turnover_rate", max_tr),
                   ("min_float_market_cap", min_cap), ("max_float_market_cap", max_cap)):
        if v is not None:
            _set(key, v)
    for key, v in values.items():
        _set(key, v)
    for key in ("max_positions", "universe_top_n", "min_list_days", "volume_ladder_segments",
                "market_ma_days", "weak_market_adv_min", "breadth_block_below",
                "sector_top_n", "sector_bottom_n"):
        v = getattr(body, key)
        if v is not None:
            _set(key, int(v))
    for key in ("require_minute_bars", "exclude_st", "exclude_suspended",
                "exclude_limit_locked", "gap_protect_enabled", "gap_buffer_enabled",
                "vwap_exit_enabled", "market_filter_enabled", "market_breadth_required",
                "open_momentum_enabled", "sector_enabled"):
        v = getattr(body, key)
        if v is not None:
            _set(key, bool(v))
    if low_open_check:
        _set("low_open_check_time", low_open_check)
    if momentum_hold:
        _set("open_momentum_hold_until", momentum_hold)
    if body.allowed_boards is not None:
        _set("allowed_boards", [str(b).upper() for b in body.allowed_boards])

    # ---- 时刻变更联动调度（选股→tail_pick_select；离场窗口起点→tail_pick_exit）----
    if select_time:
        s.set("scheduler.jobs.tail_pick_select", select_time)
        applied.append("scheduler.jobs.tail_pick_select")
    if exit_start:
        s.set("scheduler.jobs.tail_pick_exit", exit_start)
        applied.append("scheduler.jobs.tail_pick_exit")

    if not applied:
        raise HTTPException(400, "未提交任何需要修改的参数")
    save_settings(s)

    # ---- 热更新常驻调度器（拿不到实例时提示重启即可）----
    reloaded = False
    sched = getattr(getattr(request.app, "state", None), "scheduler", None)
    if sched is not None:
        try:
            reloaded = sched.reload()
        except Exception as exc:                            # noqa: BLE001
            raise HTTPException(500, f"配置已保存但调度器热更新失败: {exc}")
    return {"ok": True, "applied": applied, "reloaded": reloaded,
            "hint": "已生效（job 内实时读配置，下次触发即按新参数运行）" if reloaded
                    else "配置已保存，重启后端后生效"}


# ==================================================================== 独立回测
class TailPickBacktestIn(BaseModel):
    start: str
    end: str | None = None
    cash: float = 1_000_000.0


@router.post("/backtest")
def run_backtest(body: TailPickBacktestIn, mode: str = Query("paper")):
    """尾盘选股法独立回测（后台 job，/api/jobs/{id} 轮询）。

    与 CLI ``tailpick backtest`` 同款数据真实性护栏：检测到 MockProvider 直接拒绝，
    严禁用虚拟数据得出业绩结论；无分钟线时降级日线近似并标注【非真实业绩】。
    """
    try:
        start = date.fromisoformat(body.start)
        end = date.fromisoformat(body.end) if body.end else date.today()
    except ValueError as exc:
        raise HTTPException(400, f"日期格式应为 YYYY-MM-DD: {exc}")
    if start >= end:
        raise HTTPException(400, f"回测区间非法：{start} 不早于 {end}")

    job = ctx.new_job("tailpick_backtest")

    def _run():
        # 回测为虚拟撮合不下真单，live 观察期锁定时自动降级 paper
        c = ctx.make_ctx_research(mode)
        provider_names = list(getattr(c.hub, "providers", {}).keys())
        if "mock" in provider_names:
            raise RuntimeError(
                "检测到 MockProvider（虚拟标的+随机行情），尾盘回测已拒绝。"
                "请切到 paper 模式（需 qmt/akshare 数据源）后重试。")
        from qmt_trade.strategies.tail_pick import TailPickBacktester, TailPickConfig

        cfg = TailPickConfig.from_settings(c.settings)
        bt = TailPickBacktester(c.settings, c.hub, initial_cash=body.cash,
                                config=cfg, require_minute=None)
        result = bt.run(start, end)
        if not result.metrics:
            return {"has_metrics": False,
                    "error": "; ".join((result.details or ["未知原因"])[:3])}
        m = dict(result.metrics or {})
        m["data_mode"] = "real(" + ",".join(provider_names) + ")"
        m["minute_available"] = result.minute_available
        return {
            "has_metrics": bool(result.metrics),
            "metrics": m,
            "minute_available": result.minute_available,
            "trades": len(result.trades),
            "closed_trades": len(result.closed_trades),
            "equity_curve": (result.equity_curve or [])[:1000],
            "closed_details": (result.closed_trades or [])[:200],
            "details": (result.details or [])[:300],
            "error": None if result.metrics else
                     "; ".join((result.details or [])[:3]),
        }

    ctx.spawn(job, _run)
    return {"job_id": job.id, "kind": "tailpick_backtest",
            "start": body.start, "end": end.isoformat()}
