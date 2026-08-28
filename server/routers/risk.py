"""风控管理：阈值（Gate1/2/3）读写 + 总开关镜像。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

import server.context as ctx
from server.context import load_settings_editor, save_settings

router = APIRouter(prefix="/risk", tags=["risk"])


def _ctx(mode: str = Query("paper")):
    return ctx.make_ctx(mode)


@router.get("/gates")
def get_gates(mode: str = Query("paper")):
    c = _ctx(mode)
    # 阈值必须从配置文件现读，不能走 c.settings：那是 lru_cache 全局单例，
    # 保存后仍返回旧快照，页面会"保存成功但值没变"。
    s = load_settings_editor()
    return {
        "gate1": s.section("risk.gate1"),
        "gate2": s.section("risk.gate2"),
        "gate3": s.section("risk.gate3"),
        "kill_mode": c.killswitch.mode.value,
        "kill_reason": c.killswitch.reason,
    }


@router.put("/gates")
def put_gates(body: dict, mode: str = Query("paper")):
    """body: { "risk.gate1.max_positions": 10, ... } 点分路径 → 值。"""
    s = load_settings_editor()
    applied = []
    for path, value in body.items():
        if not path.startswith("risk."):
            raise HTTPException(400, f"只允许修改 risk.* 配置: {path}")
        s.set(path, value)
        applied.append(path)
    save_settings(s)
    return {"ok": True, "applied": applied}


@router.post("/killswitch/reset")
def reset_killswitch(request: Request, reason: str = Query("WebUI 人工恢复"),
                     mode: str = Query("paper")):
    """把总开关恢复为 NORMAL（人工确认风险已解除后使用）。

    优先操作常驻调度器持有的 ctx，确保内存态与落库态同时恢复；
    调度器不存在时退回新建 ctx（只改落库态）。
    """
    runner = getattr(request.app.state, "runner", None)
    c = runner.ctx if runner is not None else _ctx(mode)
    c.killswitch.reset(reason=reason)
    return {"ok": True, "kill_mode": c.killswitch.mode.value,
            "kill_reason": c.killswitch.reason}
