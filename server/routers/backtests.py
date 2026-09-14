"""Unified durable backtest API. Legacy entrypoints delegate here."""
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import server.context as ctx

router = APIRouter(tags=["backtests"])


class BacktestRequest(BaseModel):
    strategy: str = "balanced"
    start: date
    end: date = Field(default_factory=date.today)
    cash: float = Field(default=1_000_000, gt=0, allow_inf_nan=False)
    version_id: str | None = None
    instance_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    top_n: int = Field(default=10, ge=1, le=1000)
    warmup: int = Field(default=250, ge=0, le=2000)
    llm: bool = False


@router.get("/strategies")
def strategies():
    from qmt_trade.core.strategies import list_standalone_strategies, list_strategy_profiles
    return {"strategies": [{**s, "backtest_available": True} for s in list_strategy_profiles() + list_standalone_strategies()]}


@router.post("/backtests", status_code=202)
def submit(body: BacktestRequest, mode: str = Query("paper")):
    from qmt_trade.core.config import get_settings
    settings = get_settings().clone()
    spec = body.model_dump(mode="json")
    if not body.instance_id and not body.version_id:
        from .strategy import _instances
        active = next((i for i in _instances(mode) if i["strategy_id"] == body.strategy and i.get("enabled") and i.get("active_version")), None)
        if active:
            body = body.model_copy(update={"instance_id": active["id"]})
    if body.instance_id or body.version_id:
        from .strategy import _instances
        version_id = body.version_id
        instance_id = body.instance_id
        if version_id and ":" in version_id:
            instance_id, version_id = version_id.split(":", 1)
        item = next((i for i in _instances(mode) if i["id"] == instance_id and i["strategy_id"] == body.strategy), None)
        if not item:
            raise HTTPException(422, "策略实例不存在")
        version_id = version_id or item.get("active_version")
        version = next((v for v in item["versions"] if v["id"] == version_id), None)
        if not version:
            raise HTTPException(422, "已发布策略版本不存在")
        spec["instance_id"], spec["version_id"] = instance_id, version_id
        settings = settings.merged({"strategies": {body.strategy: version["params"]}})
    try:
        row = ctx.backtest_service().submit(spec, settings, idempotency_key=body.idempotency_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"job_id": row["id"], "kind": row["kind"], "status": row["status"]}


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str):
    row = ctx.job_repository().cancel(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    return {"id": job_id, "status": row["status"], "cancel_requested": row["cancel_requested"]}


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry(job_id: str):
    try:
        row = ctx.backtest_service().retry(job_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"job_id": row["id"], "status": row["status"]}


def downsample(points, budget):
    """Preserve first/last and per-bucket extrema over the complete interval."""
    if len(points) <= budget:
        return points
    selected = {0, len(points) - 1}
    buckets = max(1, (budget - 2) // 2)
    for bucket in range(buckets):
        begin = 1 + (len(points) - 2) * bucket // buckets
        end = 1 + (len(points) - 2) * (bucket + 1) // buckets
        indices = range(begin, end)
        if begin < end:
            selected.add(min(indices, key=lambda i: points[i]["equity"]))
            selected.add(max(indices, key=lambda i: points[i]["equity"]))
    return [points[i] for i in sorted(selected)]


@router.get("/backtests/{job_id}/report")
def report(job_id: str, points: int = Query(1000, ge=4, le=10000)):
    result = ctx.job_repository().report(job_id)
    if result is None:
        raise HTTPException(404, "报告尚未生成")
    result = dict(result)
    trades = result.pop("trades", [])
    result["trade_count"] = result.get("trade_count", len(trades))
    result["equity_curve"] = downsample(result.get("equity_curve", []), points)
    result.pop("settings_snapshot", None)
    return result


@router.get("/backtests/{job_id}/trades")
def trades(job_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    repository = ctx.job_repository().backtests
    if repository.db.scalar("SELECT count(*) FROM backtest_runs WHERE id=?", [job_id]):
        return {"total": repository.db.scalar("SELECT count(*) FROM backtest_trades WHERE run_id=?", [job_id]),
                "items": repository.trades(job_id, offset, limit)}
    result = ctx.job_repository().report(job_id)
    if result is None:
        raise HTTPException(404, "报告不存在")
    rows = result.get("trades", [])
    return {"total": len(rows), "items": rows[offset:offset + limit]}


@router.get("/notifications")
def notifications(limit: int = Query(50, ge=1, le=100)):
    return ctx.job_repository().notifications(limit)


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: str):
    ctx.job_repository().mark_read(notification_id)
    return {"ok": True}
