"""CLI requests go to the database owner instead of opening its runtime file."""
import json
from datetime import date

import requests


def execute(args):
    base = args.server.rstrip("/") + "/api"
    mode = {"mode": args.mode}
    def call(method, path, body=None, **params):
        response = requests.request(method, base + path, params={**mode, **params}, json=body, timeout=60)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        return response.json()
    command = args.command
    if command == "backtest":
        result = call("POST", "/backtests", {"strategy": args.strategy or "balanced", "start": args.start,
                      "end": args.end or date.today().isoformat(), "cash": args.cash, "top_n": args.top,
                      "warmup": args.warmup, "llm": args.llm})
    elif command == "killswitch":
        action = next((a for a in ("engage", "flatten", "reset") if getattr(args, a, None)), None)
        result = call("POST", "/killswitch", {"action": action, "reason": getattr(args, action)}) if action else call("GET", "/killswitch")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if action or result.get("mode") == "NORMAL" else 1
    elif command == "health":
        result = call("GET", "/health", notify=args.notify)
    elif command == "run":
        if args.replay:
            raise ValueError("历史调度回放请在停止服务后使用 --offline；在线服务不重放历史交易")
        result = call("POST", "/scheduler/run", name=args.once, trade_date=args.date) if args.once else call("GET", "/scheduler/jobs")
    elif command == "select":
        result = call("POST", "/selection/run", {"date": args.date, "top_n": args.top})
    elif command == "reconcile":
        result = call("POST", "/trade/reconcile/ack", {"trade_date": args.date, "operator": args.operator, "note": args.ack}) if args.ack else call("GET", "/trade/reconcile", date=args.date)
    elif command == "evolve":
        result = call("POST", "/strategy/evolve", {"date": args.date})
    elif command == "report":
        result = call("POST", "/scheduler/run", name="review", trade_date=args.date)
    else:
        raise ValueError("该命令需要停止服务后以 --offline 运行")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
