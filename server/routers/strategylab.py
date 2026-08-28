"""策略实验室：独立策略（打板/二板/尾盘低吸/趋势买点 + 尾盘选股）统一操作面板。

- ``GET /strategylab/status``：策略清单（name/summary/enabled/关键配置）+ 最近一次回测摘要；
- ``POST /strategylab/backtest``：后台跑指定独立策略的一年/区间回测（同 mock 数据护栏），
  轮询 ``/api/jobs/{jid}`` 取结果，结果摘要落 ``system_state: strategylab:bt:<sid>:latest``。

与现有 tail_pick 面板正交：不触碰任何既有策略配置，只读 settings.yaml 的
``strategies.<sid>`` 段并写回 ``enabled``。
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import server.context as ctx
from server.context import load_settings_editor, save_settings

router = APIRouter(prefix="/strategylab", tags=["strategylab"])


def _json_or(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return default


def _active_versions(mode: str) -> dict[str, list[dict]]:
    """按注册策略返回可用于实验的已发布实例版本。"""
    raw = ctx.make_ctx_research(mode).repos.system.get("strategy_instances", "[]") or "[]"
    instances = _json_or(raw, [])
    out: dict[str, list[dict]] = {}
    for item in instances if isinstance(instances, list) else []:
        sid, active = item.get("strategy_id"), item.get("active_version")
        version = next((v for v in item.get("versions", []) if v.get("id") == active), None)
        if sid and version:
            out.setdefault(sid, []).append({
                "value": f"{item.get('id')}:{version['id']}",
                "label": f"{item.get('name') or sid} / {version['id']}",
                "params": version.get("params") or {},
            })
    return out


@router.get("/status")
def status(mode: str = Query("paper")):
    """策略清单 + 启用状态 + 最近回测摘要 + 调度任务状态。"""
    from qmt_trade.core.strategies import list_standalone_strategies

    c = ctx.make_ctx(mode)
    s = load_settings_editor()
    # 调度任务状态（strategylab_open / strategylab_run 最近执行）
    jobs = {}
    for name in ("strategylab_open", "strategylab_run"):
        jobs[name] = {
            "time": s.section("scheduler.jobs") or {},
            "last_run": c.shared_repos.system.get(f"job:{name}:last_run"),
            "last_status": c.shared_repos.system.get(f"job:{name}:last_status"),
            "last_error": c.shared_repos.system.get(f"job:{name}:last_error"),
        }
    jobs_times = {k: v for k, v in (s.section("scheduler.jobs") or {}).items()
                  if k in ("strategylab_open", "strategylab_run")}
    # ETF T+0 独立盘中任务状态（新前端展示用；旧 dist 忽略未知字段，零影响）
    etf0_jobs = {k: v for k, v in (s.section("scheduler.jobs") or {}).items()
                 if k in ("etf_t0_start", "etf_t0_end", "etf_t0_interval_seconds")}
    etf0_job = {
        "time": etf0_jobs,
        "last_run": c.shared_repos.system.get("job:etf_t0_intraday:last_run"),
        "last_status": c.shared_repos.system.get("job:etf_t0_intraday:last_status"),
        "last_error": c.shared_repos.system.get("job:etf_t0_intraday:last_error"),
    }
    out = []
    versions = _active_versions(mode)
    for st in list_standalone_strategies():
        sid = st["id"]
        cfg = s.section(f"strategies.{sid}") or {}
        last = _json_or(c.shared_repos.system.get(f"strategylab:bt:{sid}:latest"), None)
        out.append({
            **st,
            "enabled": bool(cfg.get("enabled", False)),
            "versions": versions.get(sid, []),
            "key_params": {
                k: cfg.get(k) for k in ("max_positions", "position_fraction", "stop_pct",
                                        "max_hold_days", "min_boards", "max_boards",
                                        "pattern", "take_profit1", "take_profit2",
                                        "symbols", "base_fraction", "t_slice_ratio",
                                        "sell_dev_threshold", "buy_dev_threshold",
                                        "grid_step", "stop_pct",
                                        "max_trades_per_symbol_per_day")
                if k in cfg
            },
            "last_backtest": last,
        })
    return {"mode": mode, "strategies": out, "jobs": jobs, "jobs_times": jobs_times,
            "etf_t0_job": etf0_job}


class StrategyBacktestIn(BaseModel):
    strategy: str
    start: str
    end: str | None = None
    cash: float = 1_000_000.0
    version_id: str | None = None


@router.post("/backtest")
def run_backtest(body: StrategyBacktestIn, mode: str = Query("paper")):
    """后台跑独立策略回测（mock 拒绝；结果摘要落 system_state）。"""
    from qmt_trade.core.strategies import (
        STANDALONE_STRATEGIES, build_standalone_backtester,
    )

    if body.strategy not in STANDALONE_STRATEGIES:
        raise HTTPException(400, f"未知策略 {body.strategy}，可选 {STANDALONE_STRATEGIES}")
    version_params: dict = {}
    if body.version_id:
        version = next((v for v in _active_versions(mode).get(body.strategy, [])
                        if v["value"] == body.version_id), None)
        if version is None:
            raise HTTPException(422, "所选策略配置版本不存在或未发布")
        version_params = version["params"]
    try:
        start = date.fromisoformat(body.start)
        end = date.fromisoformat(body.end) if body.end else date.today()
    except ValueError as exc:
        raise HTTPException(400, f"日期格式应为 YYYY-MM-DD: {exc}")
    if start >= end:
        raise HTTPException(400, f"回测区间非法：{start} 不早于 {end}")

    job = ctx.new_job(f"strategylab_{body.strategy}")

    def _run():
        c = ctx.make_ctx_research(mode)
        provider_names = list(getattr(c.hub, "providers", {}).keys())
        if "mock" in provider_names:
            raise RuntimeError(
                "检测到 MockProvider（虚拟标的+随机行情），独立策略回测已拒绝。"
                "请切到 paper 模式（需 qmt/akshare 数据源）后重试。")
        bt = build_standalone_backtester(body.strategy, c.settings, c.hub,
                                         initial_cash=body.cash)
        if version_params:
            known = set(getattr(bt.config_class, "__dataclass_fields__", {}))
            invalid = set(version_params) - known
            if invalid:
                raise RuntimeError(f"配置版本含当前策略不支持的参数: {', '.join(sorted(invalid))}")
            bt.config = bt.config_class(**{**bt.config.__dict__, **version_params})
        result = bt.run(start, end)
        if not result.metrics:
            return {"has_metrics": False,
                    "error": "; ".join((result.details or ["未知原因"])[:3])}
        m = dict(result.metrics or {})
        m["data_mode"] = "real(" + ",".join(provider_names) + ")"
        summary = {
            "metrics": m,
            "cost": result.cost_attribution or {},
            "n_trades": len(result.trades),
            "n_closed": len(result.closed_trades),
            "equity_curve": (result.equity_curve or [])[:500],
            "version_id": body.version_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "cash": body.cash,
            "strategy": body.strategy,
            "run_at": date.today().isoformat(),
        }
        try:
            c.shared_repos.system.set(
                f"strategylab:bt:{body.strategy}:latest",
                json.dumps(summary, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 摘要落库失败不阻塞回测返回
            pass
        return {"has_metrics": True, "metrics": m, "cost": summary["cost"],
                "n_trades": summary["n_trades"], "n_closed": summary["n_closed"]}

    ctx.spawn(job, _run)
    return {"job_id": job.id, "kind": f"strategylab_{body.strategy}",
            "strategy": body.strategy, "start": body.start, "end": end.isoformat()}


class StrategyScanIn(BaseModel):
    strategy: str
    start: str
    end: str | None = None
    cash: float = 1_000_000.0
    grid: dict[str, list] = {}
    version_id: str | None = None


@router.post("/scan")
def run_scan(body: StrategyScanIn, mode: str = Query("paper")):
    """对注册策略的有限参数网格发起后台回测扫描。"""
    from itertools import product
    from qmt_trade.core.strategies import STANDALONE_STRATEGIES, build_standalone_backtester

    if body.strategy not in STANDALONE_STRATEGIES:
        raise HTTPException(400, "未知策略")
    try:
        start, end = date.fromisoformat(body.start), date.fromisoformat(body.end) if body.end else date.today()
    except ValueError as exc:
        raise HTTPException(400, f"日期格式应为 YYYY-MM-DD: {exc}")
    if start >= end or not body.grid or any(not isinstance(v, list) or not v for v in body.grid.values()):
        raise HTTPException(422, "请提供非空参数网格及合法回测区间")
    keys, values = list(body.grid), list(body.grid.values())
    combos = [dict(zip(keys, item)) for item in product(*values)]
    if len(combos) > 30:
        raise HTTPException(422, "参数组合最多 30 个")
    job = ctx.new_job(f"strategylab_scan_{body.strategy}")

    def _run():
        c = ctx.make_ctx_research(mode)
        if "mock" in getattr(c.hub, "providers", {}):
            raise RuntimeError("检测到 MockProvider，参数扫描已拒绝")
        base = {}
        if body.version_id:
            version = next((v for v in _active_versions(mode).get(body.strategy, []) if v["value"] == body.version_id), None)
            if version is None:
                raise RuntimeError("所选策略配置版本不存在或未发布")
            base = version["params"]
        rows = []
        for params in combos:
            bt = build_standalone_backtester(body.strategy, c.settings, c.hub, initial_cash=body.cash)
            known = set(getattr(bt.config_class, "__dataclass_fields__", {}))
            invalid = (set(base) | set(params)) - known
            if invalid:
                raise RuntimeError(f"参数不受当前策略支持: {', '.join(sorted(invalid))}")
            bt.config = bt.config_class(**{**bt.config.__dict__, **base, **params})
            result = bt.run(start, end)
            rows.append({"params": params, "metrics": result.metrics or {}, "n_closed": len(result.closed_trades)})
        rows.sort(key=lambda r: r["metrics"].get("total_return", float("-inf")), reverse=True)
        payload = {"strategy": body.strategy, "start": start.isoformat(), "end": end.isoformat(),
                   "version_id": body.version_id, "rows": rows, "run_at": date.today().isoformat()}
        c.shared_repos.system.set(f"strategylab:scan:{body.strategy}:latest", json.dumps(payload, ensure_ascii=False))
        return payload

    ctx.spawn(job, _run)
    return {"job_id": job.id, "kind": f"strategylab_scan_{body.strategy}", "combinations": len(combos)}


@router.get("/{sid}/report")
def report(sid: str, mode: str = Query("paper")):
    """生成可展示、可追溯的实验报告（回测及最近参数扫描摘要）。"""
    from qmt_trade.core.strategies import STANDALONE_STRATEGIES
    if sid not in STANDALONE_STRATEGIES:
        raise HTTPException(400, "未知策略")
    c = ctx.make_ctx_research(mode)
    backtest = _json_or(c.shared_repos.system.get(f"strategylab:bt:{sid}:latest"), None)
    scan = _json_or(c.shared_repos.system.get(f"strategylab:scan:{sid}:latest"), None)
    if not backtest and not scan:
        raise HTTPException(404, "尚无实验结果，请先运行回测或参数扫描")
    return {"strategy": sid, "backtest": backtest, "scan": scan,
            "generated_at": date.today().isoformat()}


@router.post("/{sid}/paper-candidate")
def make_paper_candidate(sid: str, mode: str = Query("paper")):
    """将已有真实数据回测标记为模拟盘候选；不自动启用或下单。"""
    if mode != "paper":
        raise HTTPException(422, "模拟盘候选只能在 paper 模式创建")
    c = ctx.make_ctx_research(mode)
    backtest = _json_or(c.shared_repos.system.get(f"strategylab:bt:{sid}:latest"), None)
    if not backtest or not backtest.get("metrics"):
        raise HTTPException(422, "需要先完成有效回测")
    candidate = {"strategy": sid, "source": "strategylab_backtest", "created_at": date.today().isoformat(),
                 "status": "candidate", "backtest": backtest}
    c.shared_repos.system.set(f"strategylab:paper_candidate:{sid}", json.dumps(candidate, ensure_ascii=False))
    return {"ok": True, "candidate": candidate}


class StrategyEnableIn(BaseModel):
    enabled: bool


@router.put("/{sid}/enabled")
def set_enabled(sid: str, body: StrategyEnableIn):
    """切换策略启用开关（只写 strategies.<sid>.enabled）。"""
    from qmt_trade.core.strategies import STANDALONE_STRATEGIES

    if sid not in STANDALONE_STRATEGIES:
        raise HTTPException(400, f"未知策略 {sid}")
    s = load_settings_editor()
    s.set(f"strategies.{sid}.enabled", bool(body.enabled))
    save_settings(s)
    return {"ok": True, "sid": sid, "enabled": bool(body.enabled)}
