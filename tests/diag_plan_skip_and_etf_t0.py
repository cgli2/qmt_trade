"""诊断脚本（只读）：定位「[SKIP] plan 无可用 Intent」与 ETF T+0 不下单的全部卡点。

用法：python tests/diag_plan_skip_and_etf_t0.py [mode] [trade_date]
默认 mode=live，trade_date=今天。全部通过运行中的 API 读取，不写库、不下单。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date

import requests

BASE = "http://127.0.0.1:7099/api"
MODE = sys.argv[1] if len(sys.argv) > 1 else "live"
DAY = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()


def get(path: str, **params):
    params.setdefault("mode", MODE)
    try:
        r = requests.get(BASE + path, params=params, timeout=30)
    except Exception as exc:                                    # noqa: BLE001
        return {"__error__": f"{type(exc).__name__}: {exc}"}
    if r.status_code != 200:
        return {"__error__": f"HTTP {r.status_code}", "__body__": r.text[:500]}
    try:
        return r.json()
    except Exception:                                           # noqa: BLE001
        return {"__error__": "非 JSON 响应", "__body__": r.text[:500]}


def title(s: str) -> None:
    print("\n" + "=" * 78)
    print(s)
    print("=" * 78)


def dump(obj, limit: int = 4000) -> None:
    txt = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    print(txt[:limit] + ("\n... [截断]" if len(txt) > limit else ""))


# ---------------------------------------------------------------- 1 总览/KillSwitch
title(f"1) 总览与 KillSwitch（mode={MODE}, day={DAY}）")
dump(get("/overview"))
dump(get("/killswitch"))
dump(get("/risk/gates"))

# ---------------------------------------------------------------- 2 体检项明细
title("2) 体检（health）：当前阻塞项与逐项结论")
h = get("/health", notify="false")
if "__error__" in h:
    dump(h)
else:
    print(f"healthy={h.get('healthy')} degraded={h.get('degraded')}")
    print(f"degrade_reasons={json.dumps(h.get('degrade_reasons'), ensure_ascii=False)}")
    for c in h.get("checks", []):
        print(f"  [{str(c.get('level')):>8}] {str(c.get('name')):<16} "
              f"ok={c.get('ok')} :: {c.get('message')}")
    print("  recent_jobs:")
    for j in h.get("recent_jobs", []):
        print(f"    {str(j.get('name')):<24} status={j.get('status')} last_run={j.get('last_run')}")

# ---------------------------------------------------------------- 3 Intent 链路
title("3) 选股候选池 → Intent 链路（plan 的输入）")
sel = get("/selection/final")
if "__error__" in sel:
    dump(sel)
else:
    keys = {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in sel.items()}
    print("selection/final 摘要：", json.dumps(keys, ensure_ascii=False, default=str)[:2000])
it = get("/trade/intents", date=DAY)
n_intent = len(it.get("intents") or []) if "__error__" not in it else -1
print(f"当日 Intent 数量 = {n_intent}")
dump(it if n_intent <= 3 else {"sample": (it.get("intents") or [])[:3]}, 3000)

# ---------------------------------------------------------------- 4 持仓
title("4) 当前持仓（确认 ETF T+0 底仓是否存在）")
pos = get("/trade/positions")
dump(pos, 5000)

# ---------------------------------------------------------------- 5 订单
title("5) 当日订单：status / reject_reason 分布（判断被哪一层拦）")
od = get("/trade/orders", date=DAY)
rows = od.get("orders") or [] if "__error__" not in od else []
if not rows:
    dump(od, 2000)
else:
    print(f"订单总数 = {len(rows)}")
    print("按 status：", json.dumps(Counter(str(r.get("status")) for r in rows),
                                   ensure_ascii=False))
    print("按 side：", json.dumps(Counter(str(r.get("side")) for r in rows), ensure_ascii=False))
    print("按 strategy/signal：", json.dumps(
        Counter(str(r.get("strategy") or r.get("signal") or r.get("plan_id") or "-") for r in rows),
        ensure_ascii=False))
    print("按 reject_reason（前 25）：")
    for reason, cnt in Counter(str(r.get("reject_reason") or "-") for r in rows).most_common(25):
        print(f"  {cnt:>5}  {reason[:160]}")
    print("\n非 FILLED 样本（前 12 条原始字段）：")
    for r in [x for x in rows if str(x.get("status")) != "FILLED"][:12]:
        print("  -", json.dumps(r, ensure_ascii=False, default=str)[:600])

# ---------------------------------------------------------------- 6 ETF T+0 配置与实例
title("6) ETF T+0 策略配置与实例状态（UI 是否真的启用）")
dump(get("/strategy/management"), 4000)
dump(get("/strategylab/status"), 4000)
dump(get("/tailpick/status"), 2500)

# ---------------------------------------------------------------- 7 数据源
title("7) 数据源可用性（全市场标的 / 行情新鲜度）")
dump(get("/datasource"), 3000)

# ---------------------------------------------------------------- 8 调度任务
title("8) 调度任务最近执行结果")
jb = get("/scheduler/jobs", limit="30")
if "__error__" in jb:
    dump(jb)
else:
    items = jb.get("jobs") or jb.get("items") or jb
    print(json.dumps(items, ensure_ascii=False, indent=2, default=str)[:6000])

print("\n[完成] 以上均为只读查询，未修改任何状态。")
