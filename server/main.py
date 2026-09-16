"""FastAPI 入口。前后端分离：本服务只提供 JSON API（前缀 /api），
前端由 Vite 独立构建/托管。所有业务调用复用 ``qmt_trade`` 内部模块。

注意：**本进程同时承担常驻调度器**。后端必须保持运行，定时任务才会触发；
服务启动时若发现当日已有错过的任务（如盘后才开机），会按序补跑。"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import server.context as ctx
from server.routers import (backtest, backtests, config, datasource, event, llm, market,
                            memory, notify, overview, report, risk, selection,
                            strategy, strategylab, tail_pick, trade)

# ------------------------------------------------------------------ 日志初始化
# 后端经 `uvicorn server.main:app` 启动，不走 cli.py 的 setup_logging()。若不在此
# 显式初始化，qmt_trade.* 与 server.* 都没有 handler：INFO 日志（常驻调度器启动、
# 当日错过任务的补跑链路、每个任务 run_job→_fire 的结果 render）全部丢失，只有
# WARNING+ 经 logging.lastResort 落到 stderr→backend.log。排查“data_sync/reconcile
# 到底跑没跑、返回了什么”时因此成了盲区（RC2）。console handler 写 stdout，被
# scripts/start_backend.sh 收进 logs/backend.log；幂等，重复 import 不会重复挂。
from qmt_trade.core.logging import setup_logging

_qmt_logger = setup_logging(level="INFO", console=True)
# setup_logging 只覆盖 "qmt_trade" 命名空间；server.* 走 Python 根 logger，复用同一
# 批 handler 并关掉向根传播，让后端自身日志（server.main / server.routers.*）同样可见，
# 且不会与 qmt_trade 日志重复输出。
_server_logger = logging.getLogger("server")
_server_logger.setLevel(logging.INFO)
_server_logger.propagate = False
for _h in _qmt_logger.handlers:
    if _h not in _server_logger.handlers:
        _server_logger.addHandler(_h)

logger = logging.getLogger(__name__)
DIST = Path(__file__).resolve().parent.parent / "webui" / "dist"


# ------------------------------------------------------------------ 常驻调度
#: 错过后值得补跑的 cron 任务，按依赖顺序排列（intraday 为 interval，不补）
_CATCHUP_CHAIN = ["data_sync", "regime", "selection", "research", "plan",
                  "auction_check", "reconcile", "review"]


def _ran_today(runner, name: str) -> bool:
    """该任务今天是否已留过痕（避免重复补跑；intraday 高频刷新也用它兜底）。"""
    try:
        raw = runner.ctx.repos.system.get(f"job:{name}:last_run")
        return bool(raw) and str(raw)[:10] == date.today().isoformat()
    except Exception:                                   # noqa: BLE001
        return False


def _resolve_scheduler_mode() -> str:
    """常驻调度器运行模式：默认 ``paper``（真实数据源 + 模拟撮合，绝不下真单）。

    可经 ``settings.yaml`` 的 ``scheduler.mode`` 或环境变量 ``QMT_SCHEDULER__MODE``
    改为 ``live``。此前 main.py 硬编码 ``make_ctx("paper")``，造成「UI 看 live、
    自动交易跑 paper」的错位（2026-09-15 事故：paper 侧熔断拒单，用户却在 live
    页面排查）。改为可配置，并由 ``/overview`` 把实际模式暴露给前端。
    live 仍受观察期护栏约束：未设 ``QMT_ALLOW_LIVE`` 时自动回落 paper 并告警。
    """
    from qmt_trade.core.config import get_settings
    mode = str(get_settings().get("scheduler.mode", "paper") or "paper").strip().lower()
    if mode not in ("paper", "live"):
        logger.warning("scheduler.mode=%r 非法（仅 paper/live），回落 paper", mode)
        return "paper"
    if mode == "live" and ctx.is_live_locked():
        logger.warning("scheduler.mode=live 但观察期护栏未解锁（缺 QMT_ALLOW_LIVE），回落 paper")
        return "paper"
    return mode


def _catchup(runner, sched) -> None:
    """补跑当日已错过的任务。research 很重（半小时以上），放独立线程跑，
    完成后若 plan 已跑过则再刷一次 plan，让开仓计划用上最新精选。"""
    from qmt_trade.scheduler.jobs import run_job

    now = datetime.now()
    due: list[str] = []
    for spec in sched.specs:
        if spec.kind != "cron" or spec.name not in _CATCHUP_CHAIN:
            continue
        if spec.day_of_week:                            # evolve 按周，单独判断
            from qmt_trade.scheduler.runner import _DOW
            if _DOW[now.weekday()] != spec.day_of_week:
                continue
        if now.hour * 60 + now.minute <= spec.hour * 60 + spec.minute:
            continue                                    # 还没到点，交给 cron
        if not _ran_today(runner, spec.name):
            due.append(spec.name)

    if not due:
        return
    logger.info("检测到当日错过的调度任务，开始补跑: %s", due)
    for name in due:
        if name == "research":
            continue                                    # 重型任务，最后单独跑
        try:
            res = run_job(runner, name)
            logger.info("补跑 %s", res.render())
            if not res.ok:
                logger.warning("补跑 %s 失败（%s），中止后续补跑", name, res.reason)
                return
        except Exception:                               # noqa: BLE001
            logger.exception("补跑 %s 异常，中止后续补跑", name)
            return

    if "research" in due:
        def _late_research():
            try:
                _catchup_research(runner)
            except Exception:                           # noqa: BLE001
                logger.exception("补跑 research 异常")

        threading.Thread(target=_late_research, name="catchup-research",
                         daemon=True).start()


def _catchup_research(runner) -> None:
    """补跑研判：优先用进程缓存里的候选池；缓存丢失（如盘后重启）时
    从 selection:latest 落库结果重建，与手动 /selection/research 同路径。"""
    import json as _json

    cs = runner.cache.get("candidates")
    if cs is None or getattr(cs, "is_empty", False):
        raw = runner.ctx.shared_repos.system.get("selection:latest")
        if not raw:
            logger.warning("补跑 research 跳过：无候选池落库（selection 未成功）")
            return
        payload = _json.loads(raw)
        if not payload.get("symbols"):
            logger.warning("补跑 research 跳过：候选池为空（可能 RISK_OFF）")
            return
        from server.routers.selection import _rebuild_candidateset
        cs = _rebuild_candidateset(runner.ctx, payload)
        runner.cache["candidates"] = cs
        runner.cache["regime"] = cs.regime

    res = runner.research_candidates(cs)
    logger.info("补跑 research %s", res.render())
    runner._record(res)                               # 手动/补跑路径也要留痕
    # research 晚到 → plan 可能已用旧/空精选生成，重刷一次
    if not res.skipped and _ran_today(runner, "plan"):
        from qmt_trade.scheduler.jobs import run_job
        r2 = run_job(runner, "plan")
        logger.info("research 后重刷 plan %s", r2.render())


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx.backtest_service().start()
    sched = None
    sched_mode = _resolve_scheduler_mode()
    app.state.scheduler_mode = sched_mode
    try:
        from qmt_trade.scheduler.jobs import JobRunner
        from qmt_trade.scheduler.runner import TradingScheduler

        runner = JobRunner(ctx.make_ctx(sched_mode))
        sched = TradingScheduler(runner)
        app.state.runner = runner
        app.state.scheduler = sched
        if sched.start():
            logger.info("常驻调度器已启动（mode=%s）\n%s", sched_mode, sched.describe())
            threading.Thread(target=_catchup, args=(runner, sched),
                             name="catchup", daemon=True).start()
    except Exception:                                   # noqa: BLE001
        logger.exception("常驻调度器启动失败（API 仍可单独使用）")
    yield
    if sched is not None:
        sched.shutdown(wait=False)
    ctx.backtest_service().shutdown()


app = FastAPI(title="QMT Trade WebUI API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from qmt_trade.storage.runtime import MigrationRequiredError


@app.exception_handler(MigrationRequiredError)
async def migration_required(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc), "code": "MIGRATION_REQUIRED"},
                        headers={"Retry-After": "30"})


@app.exception_handler(ctx.LiveModeLockedError)
async def _live_locked_handler(request, exc: ctx.LiveModeLockedError):
    """观察期护栏：mode=live 请求统一 403，避免 UI 误触碰出实盘账本。"""
    logger.warning("拒绝 live 模式请求: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=403, content={"detail": str(exc)})

API = "/api"

@app.middleware("http")
async def validate_mode(request: Request, call_next):
    values = request.query_params.getlist("mode")
    if any(value not in ("paper", "live") for value in values) or len(set(values)) > 1:
        return JSONResponse(status_code=422, content={"detail": "mode 只能为 paper 或 live"})
    return await call_next(request)


def _job_to_dict(j: ctx.Job) -> dict:
    row = ctx.job_repository().get(j.id)
    return {
        "id": j.id, "kind": j.kind, "status": j.status,
        "progress": j.progress, "created": j.created, "finished": j.finished,
        "result": j.result, "error": j.error,
        **{k: row[k] for k in ("stage", "heartbeat", "completed", "total", "attempt", "retry_of", "report_id", "error_code")},
        "state": row["status"],
    }


@app.get("/")
def root():
    # 前端已构建时同源托管 SPA 首页；否则返回 API 信息
    if DIST.exists():
        # 入口 HTML 不缓存：后端更新前端后浏览器自动重新校验，避免卡在旧页面
        return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})
    return {"name": "qmt_trade webui api", "docs": "/docs", "prefix": API}


@app.get(f"{API}/jobs")
def list_jobs(limit: int = 20):
    return [_job_to_dict(j) for j in ctx.list_jobs(limit)]


@app.get(f"{API}/jobs/{{jid}}")
def get_job(jid: str):
    row = ctx.job_repository().get(jid)
    j = ctx._legacy_job(row, include_result=bool(row and row["kind"] != "backtest"))
    if j is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_to_dict(j)


for _r in (backtests, overview, llm, datasource, config, risk, market, trade,
           strategy, backtest, event, notify, selection, report, memory,
           tail_pick, strategylab):
    app.include_router(_r.router, prefix=API)


# 生产/单端口场景：若前端已构建（webui/dist），由后端同源托管 SPA。
# 仅当 dist 存在时挂载，dev 模式(用 vite 5173)不受影响。
if DIST.exists():
    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # 未知 API 路径保持 404，不让 SPA 兜底掩盖接口错误
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        candidate = DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        # 入口 HTML 不缓存：后端更新前端后浏览器自动重新校验
        return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":                       # pragma: no cover
    import uvicorn
    uvicorn.run("server.main:app", host="0.0.0.0", port=7099, reload=False)
