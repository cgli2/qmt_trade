"""策略管理：策略目录（选股/交易策略详情）、策略池（状态/权重）、进化调权、复盘总结。"""

from __future__ import annotations

from datetime import date
import json
import time
import uuid

from fastapi import APIRouter, HTTPException, Query

import server.context as ctx
from server.schemas import EvolveIn, StrategyInstanceStateIn, StrategyVersionIn

router = APIRouter(prefix="/strategy", tags=["strategy"])


def _ctx(mode: str = Query("paper")):
    # 策略池/调权/进化/复盘属研究类（不下真单，常驻调度器本就跑 paper），
    # live 观察期锁定时自动降级 paper（不拦用户）
    return ctx.make_ctx_research(mode)


def _instances(mode: str = "paper") -> list[dict]:
    """实例元数据复用现有账本持久化，避免引入独立配置源。"""
    raw = _ctx(mode).repos.system.get("strategy_instances", "[]") or "[]"
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def _save_instances(items: list[dict], mode: str) -> None:
    _ctx(mode).repos.system.set("strategy_instances", json.dumps(items, ensure_ascii=False), "strategy management")


@router.get("/management")
def management(mode: str = Query("paper")):
    """策略定义只读；运行实例具有独立草稿、发布版本及启停状态。"""
    from qmt_trade.core.config import get_settings
    from qmt_trade.core.strategy_catalog import build_strategy_catalog
    catalog = build_strategy_catalog(get_settings())
    return {"definitions": catalog.get("selection", []) + catalog.get("trading", []), "instances": _instances(mode)}


@router.post("/instances/draft")
def save_draft(body: StrategyVersionIn, mode: str = Query("paper")):
    if not body.strategy_id.strip():
        raise HTTPException(422, "strategy_id is required")
    items, now = _instances(mode), time.time()
    iid = body.instance_id or f"sti_{uuid.uuid4().hex[:10]}"
    item = next((x for x in items if x["id"] == iid), None)
    if item is None:
        item = {"id": iid, "strategy_id": body.strategy_id, "name": body.name.strip() or body.strategy_id, "enabled": False, "draft": {}, "versions": [], "active_version": None, "created_at": now}
        items.append(item)
    elif item["strategy_id"] != body.strategy_id:
        raise HTTPException(422, "strategy_id cannot change for an existing instance")
    item["name"] = body.name.strip() or item["name"]
    item["draft"] = {"params": body.params, "note": body.note, "updated_at": now}
    _save_instances(items, mode)
    return {"ok": True, "instance": item}


@router.post("/instances/publish")
def publish_version(body: StrategyVersionIn, mode: str = Query("paper")):
    if not body.instance_id:
        raise HTTPException(422, "instance_id is required; save a draft first")
    items = _instances(mode)
    item = next((x for x in items if x["id"] == body.instance_id), None)
    if item is None or item["strategy_id"] != body.strategy_id:
        raise HTTPException(404, "strategy instance not found")
    params = body.params or item.get("draft", {}).get("params", {})
    version = {"id": f"v{len(item['versions']) + 1}", "params": params, "note": body.note or item.get("draft", {}).get("note", ""), "published_at": time.time()}
    item["versions"].append(version)
    item["active_version"] = version["id"]
    _save_instances(items, mode)
    return {"ok": True, "instance": item, "version": version}


@router.post("/instances/{instance_id}/rollback/{version_id}")
def rollback(instance_id: str, version_id: str, mode: str = Query("paper")):
    items = _instances(mode)
    item = next((x for x in items if x["id"] == instance_id), None)
    if item is None or not any(v["id"] == version_id for v in item.get("versions", [])):
        raise HTTPException(404, "strategy version not found")
    item["active_version"] = version_id
    _save_instances(items, mode)
    return {"ok": True, "instance": item}


@router.post("/instances/{instance_id}/enabled")
def set_enabled(instance_id: str, body: StrategyInstanceStateIn, mode: str = Query("paper")):
    items = _instances(mode)
    item = next((x for x in items if x["id"] == instance_id), None)
    if item is None:
        raise HTTPException(404, "strategy instance not found")
    item["enabled"] = body.enabled
    _save_instances(items, mode)
    return {"ok": True, "instance": item}


@router.get("/catalog")
def catalog():
    """策略目录：系统到底有哪些选股/交易策略、各自规则是什么。
    阈值全部取当前 settings 实际值，保证页面展示与真实运行一致。"""
    from qmt_trade.core.config import get_settings
    from qmt_trade.core.strategy_catalog import build_strategy_catalog
    return build_strategy_catalog(get_settings())


@router.get("/pool")
def get_pool(mode: str = Query("paper")):
    c = _ctx(mode)
    snap = c.pool.snapshot()
    return {"snapshot": snap, "killswitch": c.killswitch.mode.value}


@router.post("/rebalance")
def rebalance(mode: str = Query("paper")):
    c = _ctx(mode)
    res = c.pool.rebalance(date.today())
    c.save_pool()
    return {"ok": True, "weights": res.weights, "report": res.report()}


@router.post("/evolve")
def evolve(body: EvolveIn, mode: str = Query("paper")):
    job = ctx.new_job("evolve")

    def _run():
        c = ctx.make_ctx_research(mode)
        from qmt_trade.scheduler.jobs import JobRunner
        runner = JobRunner(c, trade_date=body.date)
        res = runner.evolve()
        c.save_pool()
        return {"ok": res.ok, "weights": res.data.get("weights", {}),
                "render": res.render()}

    ctx.spawn(job, _run)
    return {"job_id": job.id, "kind": "evolve"}


@router.post("/review")
def review(trade_date: str | None = None, mode: str = Query("paper")):
    job = ctx.new_job("review")

    def _run():
        c = ctx.make_ctx_research(mode)
        from qmt_trade.scheduler.jobs import JobRunner, run_job
        runner = JobRunner(c, trade_date=trade_date)
        res = run_job(runner, "review")
        return {"ok": res.ok, "render": res.render(), "data": res.data}

    ctx.spawn(job, _run)
    return {"job_id": job.id, "kind": "review"}
