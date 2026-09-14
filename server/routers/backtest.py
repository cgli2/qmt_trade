"""Compatibility proxy to the durable backtest service."""
from datetime import date
from fastapi import APIRouter, Query
from server.schemas import BacktestIn
from .backtests import BacktestRequest, submit
router = APIRouter(prefix="/backtest", tags=["backtest"])

@router.post("/run", status_code=202)
def run_backtest(body: BacktestIn, mode: str = Query("paper")):
    payload = body.model_dump()
    payload["end"] = payload.get("end") or date.today().isoformat()
    return submit(BacktestRequest(**payload), mode)
