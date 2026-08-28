"""WebUI API 端到端冒烟：逐个打真实 HTTP 端点，校验状态码与关键字段。

用法：先起后端 `python -m server.main`，再 `python tests/smoke_webui_api.py`。
只读为主；写操作全部在 sim 模式且写入后回滚/幂等，不污染实盘配置。
"""

from __future__ import annotations
import logging

import json
import os
import sys
import time
import urllib.error
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger(__name__)

BASE = (sys.argv[1] if len(sys.argv) > 1 else
        os.environ.get("WEBUI_BASE", "http://127.0.0.1:7099/api"))
PASS, FAIL = [], []


def call(method: str, path: str, body=None, expect=200):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            code, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read().decode()
    except Exception as exc:                          # noqa: BLE001
        FAIL.append((method, path, f"EXC {type(exc).__name__}: {exc}"))
        return None
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = raw
    if code != expect:
        FAIL.append((method, path, f"HTTP {code} != {expect}: {str(payload)[:180]}"))
        return payload
    PASS.append((method, path, code))
    return payload


def main() -> int:
    # --- 系统
    ov = call("GET", "/overview?mode=sim")
    assert ov and ov["mode"] == "sim", ov
    call("GET", "/health?mode=sim")
    call("GET", "/killswitch?mode=sim")
    call("GET", "/scheduler/jobs?mode=sim")
    call("GET", "/secrets")

    # --- LLM（只读 + 幂等回写同一份 selection）
    cfg = call("GET", "/llm/config")
    if cfg:
        call("PUT", "/llm/selection", cfg.get("selection") or {
            "strategy": "weighted", "capability_weight": 0.4,
            "health_weight": 0.4, "cost_weight": 0.2, "fallback_enabled": True})

    # --- 数据源 / 配置 / 风控
    call("GET", "/datasource?mode=sim")
    call("GET", "/config")
    call("GET", "/config/section/risk")
    call("GET", "/risk/gates?mode=sim")

    # --- 行情
    syms = call("GET", "/market/symbols?mode=sim")
    sym = (syms or {}).get("symbols", [{}])[0].get("symbol")
    if sym:
        bars = call("GET", f"/market/bars?symbols={sym}&start=2024-01-01&mode=sim")
        assert bars and bars["count"] > 0, f"bars empty for {sym}"
        call("GET", f"/market/quote?symbols={sym}&mode=sim")
    call("GET", "/market/news?mode=sim&limit=5")
    call("GET", "/market/events?mode=sim&limit=5")

    # --- 事件
    call("GET", "/event/events?mode=sim&limit=5")
    call("GET", "/event/hard-negatives?mode=sim")

    # --- 交易（只读 + sim 模拟单）
    call("GET", "/trade/positions?mode=sim")
    call("GET", "/trade/orders?mode=sim")
    call("GET", "/trade/intents?mode=sim")
    call("GET", "/trade/reconcile?mode=sim")
    call("POST", "/trade/intent?mode=live", {"symbol": sym or "600000", "action": "BUY"},
         expect=403)                                   # 实盘必须被拒
    if sym:
        call("POST", "/trade/intent?mode=sim",
             {"symbol": sym, "action": "BUY", "shares": 0, "confidence": 0.6,
              "conviction": "MEDIUM", "reason": "smoke", "stop_loss_value": 0.08})

    # --- 策略
    call("GET", "/strategy/pool?mode=sim")

    # --- 通知
    call("GET", "/notify/channels")

    # --- 回测（后台 job，轮询到结束）
    bt = call("POST", "/backtest/run?mode=sim",
              {"start": "2024-06-01", "end": "2024-09-30", "cash": 500000,
               "top_n": 5, "warmup": 120, "llm": False})
    if bt and bt.get("job_id"):
        jid = bt["job_id"]
        for _ in range(120):
            j = call("GET", f"/jobs/{jid}")
            if j and j["status"] in ("done", "error"):
                if j["status"] == "error":
                    FAIL.append(("JOB", "backtest", j.get("error")))
                else:
                    m = (j.get("result") or {}).get("metrics") or {}
                    logger.info(f"  回测指标: {json.dumps(m, ensure_ascii=False)[:200]}")
                break
            time.sleep(1)
        else:
            FAIL.append(("JOB", "backtest", "超时未完成"))
    call("GET", "/jobs?limit=5")

    logger.info(f"\n通过 {len(PASS)} 项：")
    for m, p, c in PASS:
        logger.info(f"  ✓ {m:6s} {p}  -> {c}")
    if FAIL:
        logger.info(f"\n失败 {len(FAIL)} 项：")
        for m, p, e in FAIL:
            logger.info(f"  ✗ {m:6s} {p}  -> {e}")
        return 1
    logger.info("\n全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())