"""交易管理：持仓 / 订单 / 意图（读）、盘后对账（读+人工确认）、手动下单、运行计划。

手动下单对 paper / live 均开放：无论哪种模式，Intent 都必须原样穿过三道风控闸门
（仓位/风控/KillSwitch），实盘还叠加 QMT 网关异常自动降级 REDUCE_ONLY 兜底。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query

import server.context as ctx
from qmt_trade.brain.schemas import TradeIntent
from qmt_trade.core.trading import Side
from qmt_trade.datahub.types import Adjust, Bar, Freq
from qmt_trade.features.regime import Regime, RegimeSnapshot
from server.schemas import ReconcileAck, TradeIntentIn

router = APIRouter(prefix="/trade", tags=["trade"])


def _ctx(mode: str = Query("paper")):
    return ctx.make_ctx(mode)


@router.get("/positions")
def positions(mode: str = Query("paper")):
    c = _ctx(mode)
    rows = c.repos.positions.list_all()
    source = "book"
    note = None
    if mode == "live" and not rows:
        # 实盘本地账本为空（观察期/未对账入库）时回落券商真实持仓，
        # 否则页面「持仓概要」恒显示 0 只，与账户真实情况严重不符。
        broker_rows, note = _broker_positions(c)
        if broker_rows:
            rows, source = broker_rows, "broker"
    return {"mode": c.mode, "source": source, "note": note,
            "positions": _enrich_positions(c, rows)}


@router.delete("/positions")
def reset_positions(mode: str = Query("paper")):
    """重置模拟盘账本：清空持仓与账户快照（现金回到初始资金）。实盘账本禁止。"""
    if mode == "live":
        raise HTTPException(403, "实盘账本禁止从 Web 清空（对账/审计需要完整留痕）")
    c = _ctx(mode)
    rows = c.repos.positions.list_all()
    for r in rows:
        c.repos.positions.remove(r["symbol"])
    c.db.delete("account_snapshots", "1=1")
    return {"ok": True, "removed": len(rows),
            "message": f"已清空 {len(rows)} 条模拟持仓并重置账户快照"}


@router.get("/broker")
def broker(mode: str = Query("live")):
    """券商侧实时持仓与资产（仅 live 模式有真实券商可查）。"""
    c = _ctx(mode)
    broker = c.gateway
    if not hasattr(broker, "query_positions"):
        return {"available": False,
                "message": f"{c.mode} 模式为模拟撮合，没有真实券商可查；请切换到实盘 Tab"}
    try:
        positions = broker.query_positions()
        asset = broker.query_asset()
    except Exception as exc:                                  # noqa: BLE001
        return {"available": False,
                "message": f"券商查询失败：{type(exc).__name__}: {exc}（请确认 QMT 客户端已登录且配置正确）",
                "killswitch": c.killswitch.mode.value}
    for p in positions:
        vol = int(p.get("volume") or 0)
        mv = float(p.get("market_value") or 0)
        cost = float(p.get("avg_cost") or 0)
        p["available"] = int(p.pop("can_use", 0) or 0)
        p["last_price"] = round(mv / vol, 3) if vol else None
        p["unrealized_pnl"] = round(mv - cost * vol, 2) if cost and mv else None
        p["name"] = _symbol_name(c, p.get("symbol", ""))
    return {"available": True, "mode": c.mode, "positions": positions,
            "asset": asset, "killswitch": c.killswitch.mode.value}


def _broker_positions(c):
    """券商侧持仓（字段对齐账本展示：volume/market_value/unrealized_pnl）。

    失败/不可用时返回空列表 + 说明文案，绝不抛给调用方。
    """
    broker = c.gateway
    if not hasattr(broker, "query_positions"):
        return [], None
    try:
        positions = broker.query_positions()
    except Exception as exc:                                  # noqa: BLE001
        return [], (f"实盘账本为空，券商持仓查询也未成功："
                    f"{type(exc).__name__}: {exc}（请确认 QMT 客户端已登录）")
    out = []
    for p in positions:
        vol = int(p.get("volume") or 0)
        if vol <= 0:
            continue
        mv = float(p.get("market_value") or 0)
        cost = float(p.get("avg_cost") or 0)
        out.append({
            "symbol": p.get("symbol", ""),
            "volume": vol,
            "available": int(p.get("can_use", 0) or 0),
            "avg_cost": cost,
            "last_price": round(mv / vol, 3) if vol else None,
            "market_value": mv,
            "unrealized_pnl": round(mv - cost * vol, 2) if cost and mv else None,
            "name": _symbol_name(c, p.get("symbol", "")),
        })
    note = "实盘账本暂无记录，此处展示券商实时持仓" if out else None
    return out, note


def _symbol_name(c, symbol: str) -> str:
    try:
        ins = c.hub.get_instrument(symbol)
        return getattr(ins, "name", "") or ""
    except Exception:                                         # noqa: BLE001
        return ""


def _enrich_positions(c, rows: list[dict]) -> list[dict]:
    """给本地账本持仓补展示字段：名称/市值/浮动盈亏/持有天数（不改动库内数据）。"""
    today = date.today()
    out: list[dict] = []
    for r in rows:
        r = dict(r)
        vol = int(r.get("volume") or 0)
        cost = float(r.get("avg_cost") or 0)
        last = float(r.get("last_price") or 0)
        r["market_value"] = round(vol * (last or cost), 2)
        r["unrealized_pnl"] = round((last - cost) * vol, 2) if last else None
        entry = str(r.get("entry_date") or "")[:10]
        try:
            r["holding_days"] = (today - date.fromisoformat(entry)).days
        except ValueError:
            r["holding_days"] = None
        r["name"] = _symbol_name(c, r.get("symbol", ""))
        out.append(r)
    return out


@router.get("/orders")
def orders(date_: str | None = Query(None, alias="date"), mode: str = Query("paper")):
    c = _ctx(mode)
    if date_:
        return {"orders": c.repos.orders.list_by_date(date_)}
    # 不指定日期时返回最近委托（含已成交/被拒）：模拟盘订单即时撮合成 FILLED，
    # 若只查未结挂单（list_open）页面会永远显示"无订单"。
    return {"orders": c.repos.orders.list_recent(50)}


@router.get("/intents")
def intents(date_: str | None = Query(None, alias="date"), mode: str = Query("paper")):
    c = _ctx(mode)
    d = date_ or date.today().isoformat()
    return {"intents": c.shared_repos.intents.list_by_date(d)}


@router.get("/reconcile")
def reconcile(date_: str | None = Query(None, alias="date"), mode: str = Query("paper")):
    c = _ctx(mode)
    day = date.fromisoformat(date_) if date_ else date.today()
    broker = c.gateway
    if not hasattr(broker, "query_positions"):
        return {"available": False,
                "message": f"{c.mode} 模式没有券商可对账，请用 --mode live",
                "killswitch": c.killswitch.mode.value}
    res = c.reconciler.run(day, broker)
    return {"available": True, "passed": res.passed,
            "checked": res.checked, "discrepancies": res.discrepancies,
            "render": res.render()}


@router.post("/reconcile/ack")
def reconcile_ack(body: ReconcileAck, mode: str = Query("paper")):
    c = _ctx(mode)
    ok = c.reconciler.acknowledge(
        date.fromisoformat(body.trade_date) if body.trade_date else date.today(),
        operator=body.operator, note=body.note)
    return {"ok": bool(ok), "killswitch": c.killswitch.mode.value}


@router.post("/intent")
def submit_intent(body: TradeIntentIn, mode: str = Query("paper")):
    c = _ctx(mode)
    today = date.today()
    try:
        df = c.hub.get_bars([body.symbol], freq=Freq.D1,
                            start=today - timedelta(days=120), end=today,
                            adjust=Adjust.HFQ)
    except Exception as exc:                                  # noqa: BLE001
        raise HTTPException(400, f"行情获取失败：{exc}（请确认标的代码）")
    if df is None or len(df) == 0:
        raise HTTPException(400, "无行情数据，无法构建订单（请确认标的代码）")
    last = df.iloc[-1]

    def _d(v):
        return v.date() if hasattr(v, "date") else v

    bar = Bar(
        date=_d(last["date"]), symbol=body.symbol,
        open=float(last["open"]), high=float(last["high"]),
        low=float(last["low"]), close=float(last["close"]),
        volume=float(last["volume"]),
        amount=float(last.get("amount", 0) or 0),
        prev_close=float(last.get("prev_close", 0) or 0),
        turnover_rate=float(last.get("turnover_rate", 0) or 0),
    )
    instrument = c.hub.get_instrument(body.symbol)
    sym_industry = {body.symbol: (instrument.industry if instrument else "")}
    snap = RegimeSnapshot(asof=today, regime=Regime.RANGE, max_position=0.5,
                          min_score=0.0, min_percentile=0.7)
    st = TradeIntent(
        symbol=body.symbol, action=body.action, confidence=body.confidence,
        conviction=body.conviction, entry_type="LIMIT", entry_ref_price=body.price,
        stop_loss_type=("FIXED_PCT" if body.stop_loss_type == "percent" else "STRUCTURE"),
        stop_loss_value=body.stop_loss_value, risk_budget_hint=0.6, max_weight_hint=0.12,
        time_horizon_days=10, max_holding_days=20, valid_until=today + timedelta(days=20),
        reasoning=body.reason or f"Web 控制台手动下单（{c.mode}）",
    )
    res = c.execution.submit_intent(
        st, bar=bar, market_day=today, asof=today, regime=snap,
        instrument=instrument, sym_industry=sym_industry,
        plan_id="webui", seq=0)
    return {
        "ok": res.ok, "symbol": res.symbol, "action": res.action,
        "shares": res.shares, "rejected_by": res.rejected_by,
        "reason": res.reason, "risk_violations": res.risk_violations,
        "fill": asdict(res.fill) if res.fill else None,
    }


@router.post("/plan")
def run_plan(trade_date: str | None = None, mode: str = Query("paper"),
             research: bool = False):
    from qmt_trade.scheduler.jobs import JobRunner, run_job

    c = _ctx(mode)
    runner = JobRunner(c, trade_date=trade_date)
    res = run_job(runner, "plan")
    if research:
        runner.research()
    return {"ok": res.ok, "name": res.name, "reason": res.reason,
            "rendered": res.render(), "data": res.data}
