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
from server.routers import (backtest, config, datasource, event, llm, market,
                            memory, notify, overview, report, risk, selection,
                            strategy, strategylab, tail_pick, trade)

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
    sched = None
    try:
        from qmt_trade.scheduler.jobs import JobRunner
        from qmt_trade.scheduler.runner import TradingScheduler

        runner = JobRunner(ctx.make_ctx("paper"))
        sched = TradingScheduler(runner)
        app.state.runner = runner
        app.state.scheduler = sched
        if sched.start():
            logger.info("常驻调度器已启动\n%s", sched.describe())
            threading.Thread(target=_catchup, args=(runner, sched),
                             name="catchup", daemon=True).start()
    except Exception:                                   # noqa: BLE001
        logger.exception("常驻调度器启动失败（API 仍可单独使用）")
    yield
    if sched is not None:
        sched.shutdown(wait=False)


app = FastAPI(title="QMT Trade WebUI API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ctx.LiveModeLockedError)
async def _live_locked_handler(request, exc: ctx.LiveModeLockedError):
    """观察期护栏：mode=live 请求统一 403，避免 UI 误触碰出实盘账本。"""
    logger.warning("拒绝 live 模式请求: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=403, content={"detail": str(exc)})

API = "/api"

#: 必须保持 live 锁定的链路（可下单/真实账本/风控闸门）：静默降级会让
#: 用户误以为在操作 live 账本，故宁可 403 明示。其余所有接口（含未来新增
#: 路由）由下方中间件统一降级，无需逐路由改造。
_LIVE_LOCKED_PREFIXES = (f"{API}/trade", f"{API}/risk")

_fallback_logged = False


@app.middleware("http")
async def live_lock_guard(request: Request, call_next):
    """观察期全局护栏：live 锁死时把非交易链路的 mode=live 入口参数改写为
    paper，整站只读/研究/运维类页面照常可用；交易/风控白名单保持原样由
    make_ctx 抛错 → 403。

    注意：BaseHTTPMiddleware 的 call_next 始终用原始 scope 建请求，
    必须原地改 scope["query_string"]（新构造 Request 传不进去）。"""
    global _fallback_logged
    mode = request.query_params.get("mode")
    if (mode or "").strip().lower() == "live" and ctx.is_live_locked() \
            and not request.url.path.startswith(_LIVE_LOCKED_PREFIXES):
        pairs = [(k, "paper" if k == "mode" else v) for k, v in parse_qsl(
            request.scope.get("query_string", b"").decode(), keep_blank_values=True)]
        old_qs = request.scope.get("query_string", b"")
        request.scope["query_string"] = urlencode(pairs).encode()
        if not _fallback_logged:
            logger.info("live 处于观察期锁定，非交易链路请求已全局自动降级为 paper"
                        "（交易/风控链路除外）")
            _fallback_logged = True
        logger.debug("live→paper 降级: %s %s", request.method, request.url.path)
        try:
            return await call_next(request)
        finally:
            request.scope["query_string"] = old_qs      # 还原，避免复用 scope 的副作用
    return await call_next(request)


def _job_to_dict(j: ctx.Job) -> dict:
    return {
        "id": j.id, "kind": j.kind, "status": j.status,
        "progress": j.progress, "created": j.created, "finished": j.finished,
        "result": j.result, "error": j.error,
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
    j = ctx.get_job(jid)
    if j is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_to_dict(j)


for _r in (overview, llm, datasource, config, risk, market, trade,
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
