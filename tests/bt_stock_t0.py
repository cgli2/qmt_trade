"""个股存量持仓做T（高抛低吸）一年回测。

用法：
    python scripts/bt_stock_t0.py --symbols 600519.SH,000858.SZ
    python scripts/bt_stock_t0.py --symbols 600519.SH --shares '{"600519.SH": 500}'
    python scripts/bt_stock_t0.py --symbols 600519.SH --cfg off

结果追加 logs/stock_t0_1y.txt（JSON 行）。
真实数据铁律：检测到 MockProvider 直接拒绝（与 cli.py 同铁律）。
"""
import sys
import os
import logging
import datetime
import argparse
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.WARNING)

from qmt_trade.app import build_context
from qmt_trade.strategies.stock_t0 import StockT0Backtester, StockT0Config

START, END = datetime.date(2025, 8, 19), datetime.date(2026, 8, 19)
CASH = 1_000_000.0
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "logs", "stock_t0_1y.txt")

CONFIGS = {
    "default": dict(),
    "off": dict(momentum_mode="off"),
    "filter": dict(momentum_mode="filter"),
    "confirm": dict(momentum_mode="confirm", momentum_window_min=15,
                    momentum_threshold=0.004),
    "aggressive": dict(sell_dev_threshold=0.006, grid_step=0.004, stop_pct=0.004,
                       max_trades_per_symbol_per_day=3),
    "conservative": dict(sell_dev_threshold=0.010, grid_step=0.006, stop_pct=0.006,
                         max_trades_per_symbol_per_day=1),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="600519.SH,000858.SZ",
                    help="存量持仓标的（逗号分隔）")
    ap.add_argument("--shares", default="{}",
                    help="初始持仓 JSON 字典字符串；缺省按 base_fraction 兜底")
    ap.add_argument("--cfg", default="default", choices=list(CONFIGS))
    ap.add_argument("--start", default=START.isoformat())
    ap.add_argument("--end", default=END.isoformat())
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    try:
        initial_shares = json.loads(args.shares) or {}
    except Exception:
        initial_shares = {}
    ov = CONFIGS[args.cfg]

    with build_context("paper") as ctx:
        providers = list(getattr(ctx.hub, "providers", {}).keys())
        if "mock" in providers:
            print("REJECTED: 检测到 MockProvider，拒绝回测（真实数据铁律）")
            return
        cfg = StockT0Config(symbols=symbols, initial_shares=initial_shares, **ov)
        bt = StockT0Backtester(ctx.settings, ctx.hub, initial_cash=CASH, config=cfg)
        res = bt.run(datetime.date.fromisoformat(args.start),
                     datetime.date.fromisoformat(args.end))
        m = res.metrics or {}
        rec = {
            "cfg": args.cfg, "symbols": symbols, "start": args.start, "end": args.end,
            "providers": providers,
            "total_return": m.get("total_return"), "max_dd": m.get("max_drawdown"),
            "t0_pnl": m.get("t0_pnl"), "t0_legs": m.get("t0_legs"),
            "t0_win": m.get("t0_win_legs"), "t0_loss": m.get("t0_loss_legs"),
            "t0_days": m.get("t0_days_traded"),
            "n_trades": m.get("n_trades"), "n_days": m.get("n_days"),
            "minute_available": m.get("minute_available"),
            "details": res.details,
        }
        line = json.dumps(rec, ensure_ascii=False)
        print(line)
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(line + "\n")


if __name__ == "__main__":
    main()
