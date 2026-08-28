"""参数配置：对 settings.yaml 各段做读写（风控/仓位/执行/调度/特征/选股/回测/应用）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import server.context as ctx
from server.context import load_settings_editor, save_settings
from server.schemas import SettingPatch

router = APIRouter(prefix="/config", tags=["config"])


class PatchBatch(BaseModel):
    patches: list[SettingPatch]


@router.get("")
def get_all():
    return load_settings_editor().as_dict()


@router.get("/section/{path:path}")
def get_section(path: str):
    return load_settings_editor().get(path, {})


@router.put("/patch")
def patch_many(body: PatchBatch):
    s = load_settings_editor()
    applied = []
    for p in body.patches:
        try:
            s.set(p.path, p.value)
            applied.append(p.path)
        except Exception as exc:                 # noqa: BLE001
            raise HTTPException(400, f"写入 {p.path} 失败: {exc}")
    save_settings(s)
    return {"ok": True, "applied": applied}
