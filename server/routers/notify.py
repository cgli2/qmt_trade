"""消息推送配置：读取频道（飞书/企微/钉钉等 webhook 经环境变量），发送测试。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

import server.context as ctx
from server.context import load_settings_editor, save_settings
from server.schemas import NotifyTestIn

router = APIRouter(prefix="/notify", tags=["notify"])


@router.get("/channels")
def get_channels():
    import os
    s = load_settings_editor()
    notify_cfg = (s.section("ops").get("notify") or {})
    channels = notify_cfg.get("channels", [])
    # 标记每个频道依赖的密钥是否已配置
    out = []
    for ch in channels:
        if isinstance(ch, str):
            out.append({"type": ch, "secret_set": None})
            continue
        chd = dict(ch)
        env_key = chd.get("webhook_env") or chd.get("env")
        chd["secret_set"] = bool(os.environ.get(env_key)) if env_key else None
        out.append(chd)
    return {"channels": out, "raw": notify_cfg}


@router.put("/channels")
def put_channels(body: dict):
    channels = body.get("channels")
    if not isinstance(channels, list):
        raise HTTPException(400, "channels 必须是列表")
    s = load_settings_editor()
    ops = s.section("ops") or {}
    ops.setdefault("notify", {})["channels"] = channels
    s.set("ops", ops)
    save_settings(s)
    return {"ok": True, "channels": channels}


@router.post("/test")
def test_notify(body: NotifyTestIn, mode: str = "paper"):
    # 发送测试与下单无关，live 观察期锁定时自动降级 paper
    c = ctx.make_ctx_research(mode)
    try:
        # send_test 绕过节流并返回逐通道结果，避免"console 兜底成功"掩盖真实失败
        return c.notifier.send_test(body.title, body.body, channel=body.channel or "")
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
