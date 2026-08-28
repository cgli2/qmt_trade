"""LLM 独立管理：多平台 provider / 多模型 / 场景路由 / 加权选模 / 熔断健康。

页面可对 ``config/llm.yaml`` 做增删改并落盘；API Key 只存环境变量名，写值走 /secrets。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from qmt_trade.brain.llm.manager import LLMManager
from qmt_trade.brain.llm.registry import (
    load_llm_config, parse_llm_config, save_llm_config,
)
from server.context import set_secret
from server.schemas import (LLMTestIn, ModelIn, ProviderIn, SceneIn, SelectionIn)

router = APIRouter(prefix="/llm", tags=["llm"])


def _config_dict():
    cfg = load_llm_config()
    d = cfg.to_dict()
    try:
        mgr = LLMManager.from_file()
        d["health"] = mgr.health_snapshot()
        d["manager_enabled"] = mgr.enabled
    except Exception as exc:                     # noqa: BLE001
        d["health"] = []
        d["manager_enabled"] = False
        d["manager_error"] = f"{type(exc).__name__}: {exc}"
    return d


def _mutate(apply_fn) -> dict:
    """load → 修改 dict → 重建 → 落盘 → 返回最新配置。"""
    cfg = load_llm_config()
    d = cfg.to_dict()
    apply_fn(d)
    save_llm_config(parse_llm_config(d))
    return _config_dict()


@router.get("/config")
def get_config():
    return _config_dict()


@router.put("/enabled")
def set_enabled(body: dict):
    enabled = bool(body.get("enabled", True))
    return _mutate(lambda d: d.__setitem__("enabled", enabled))


@router.post("/providers")
def upsert_provider(body: ProviderIn):
    data = body.model_dump()

    def apply(d):
        lst = d.setdefault("providers", [])
        for i, p in enumerate(lst):
            if p["id"] == data["id"]:
                lst[i] = data
                return
        lst.append(data)
    return _mutate(apply)


@router.delete("/providers/{pid}")
def delete_provider(pid: str):
    def apply(d):
        d["providers"] = [p for p in d.get("providers", []) if p["id"] != pid]
    return _mutate(apply)


@router.post("/models")
def upsert_model(body: ModelIn):
    data = body.model_dump()

    def apply(d):
        lst = d.setdefault("models", [])
        for i, m in enumerate(lst):
            if m["id"] == data["id"]:
                lst[i] = data
                return
        lst.append(data)
    return _mutate(apply)


@router.delete("/models/{mid}")
def delete_model(mid: str):
    def apply(d):
        d["models"] = [m for m in d.get("models", []) if m["id"] != mid]
    return _mutate(apply)


@router.post("/scenes")
def upsert_scene(body: SceneIn):
    data = body.model_dump()

    def apply(d):
        scenes = d.setdefault("scenes", {})
        scenes[data["id"]] = {
            "prefer": data["prefer"],
            "candidates": data["candidates"],
            "description": data["description"],
        }
    return _mutate(apply)


@router.delete("/scenes/{sid}")
def delete_scene(sid: str):
    def apply(d):
        d.get("scenes", {}).pop(sid, None)
    return _mutate(apply)


@router.put("/selection")
def update_selection(body: SelectionIn):
    data = body.model_dump()

    def apply(d):
        d["selection"] = data
    return _mutate(apply)


@router.post("/test")
def test_call(body: LLMTestIn):
    """用当前管理层真实发一次请求，验证 provider / 场景选模是否可用。"""
    try:
        mgr = LLMManager.from_file()
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(500, f"管理层装配失败: {exc}")
    if not mgr.enabled:
        raise HTTPException(409, "LLM 未启用（enabled=false，当前为 mock 兜底）")
    try:
        resp = mgr.complete(
            body.prompt,
            model=body.model,
            scene=body.scene,
            temperature=body.temperature,
        )
        return {
            "ok": True,
            "content": resp.content,
            "model": resp.model,
            "cost_cny": resp.cost_cny,
            "latency_ms": resp.latency_ms,
            "cached": resp.cached,
            "prompt_tokens": resp.prompt_tokens,
            "completion_tokens": resp.completion_tokens,
        }
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
