"""数据源管理：优先级、熔断配置、运行时健康度。"""

from __future__ import annotations

from fastapi import APIRouter, Query

import server.context as ctx
from server.context import load_settings_editor, save_settings

router = APIRouter(prefix="/datasource", tags=["datasource"])


def _ctx(mode: str = Query("paper")):
    # 数据源配置/健康属运维查看类，live 观察期锁定时自动降级 paper（不拦用户）
    return ctx.make_ctx_research(mode)


@router.get("")
def list_sources(mode: str = Query("paper")):
    c = _ctx(mode)
    # 配置从文件现读（c.settings 是启动快照，保存后会读到旧值）；健康度走运行上下文
    s = load_settings_editor()
    priority = s.section("datahub.priority")
    cb = s.section("datahub.circuit_breaker")
    health = []
    try:
        health = c.hub.health_snapshot()
    except Exception as exc:                     # noqa: BLE001
        health = [{"name": "hub", "healthy": False, "error": str(exc)}]
    return {"priority": priority, "circuit_breaker": cb, "health": health}


@router.put("/priority")
def set_priority(body: dict, mode: str = Query("paper")):
    priority = body.get("priority")
    if not isinstance(priority, dict):
        from fastapi import HTTPException
        raise HTTPException(400, "priority 必须是 {层级: [源名,...]} 映射")
    s = load_settings_editor()
    s.set("datahub.priority", priority)
    save_settings(s)
    return {"ok": True, "priority": s.section("datahub.priority")}
