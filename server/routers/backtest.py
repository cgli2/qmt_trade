"""回测管理：后台跑回测（避免阻塞请求），结果通过 /api/jobs/{id} 轮询。"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query

import server.context as ctx
from server.schemas import BacktestIn

router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.post("/run")
def run_backtest(body: BacktestIn, mode: str = Query("paper")):
    try:
        start = date.fromisoformat(body.start)
        end = date.fromisoformat(body.end) if body.end else date.today()
    except ValueError as exc:
        raise HTTPException(400, f"日期格式应为 YYYY-MM-DD: {exc}")

    job = ctx.new_job("backtest")

    def _run():
        # 回测为虚拟撮合不下真单，live 观察期锁定时自动降级 paper
        c = ctx.make_ctx_research(mode)
        from qmt_trade.backtest.engine import BacktestEngine
        brain = c.brain if body.llm else None
        engine = BacktestEngine(
            c.settings, c.hub,
            initial_cash=body.cash, top_n=body.top_n,
            fixed_start=start - timedelta(days=body.warmup),
            brain=brain,
        )
        result = engine.run(start, end)
        # 数据真实性护栏（与 CLI 同款）：标记 data_mode，杜绝把 mock 当成真回测
        provider_names = list(getattr(c.hub, "providers", {}).keys())
        is_mock = "mock" in provider_names
        if result.metrics is not None:
            result.metrics["data_mode"] = (
                "sim_mock(虚拟数据)" if is_mock else "real(" + ",".join(provider_names) + ")")
        return {
            "metrics": result.metrics,
            "data_mode": (result.metrics or {}).get("data_mode", "unknown"),
            "trades": len(result.trades),
            "closed_trades": len(result.closed_trades),
            "equity_curve": (result.equity_curve or [])[:1000],
            "details": (result.details or [])[:300],
            "has_metrics": bool(result.metrics),
            "error": "; ".join(result.details[:3]) if not result.metrics else None,
        }

    ctx.spawn(job, _run)
    return {"job_id": job.id, "kind": "backtest",
            "start": body.start, "end": end.isoformat()}
