"""一年回测（2025-08-18 ~ 2026-08-18）：off / filter / confirm 对比。

用法：python scripts/bt_etf_t0_1y.py --cfg off|filter|confirm_t04|confirm_t10|confirm_t15
结果追加 logs/etf_t0_1y.txt
"""
import sys, os, logging, datetime, argparse, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.WARNING)

from qmt_trade.app import build_context
from qmt_trade.strategies.etf_t0 import ETFT0Backtester, ETFT0Config

START, END = datetime.date(2025, 8, 18), datetime.date(2026, 8, 18)
CASH = 1_000_000.0
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "logs", "etf_t0_1y.txt")

CONFIGS = {
    "off":            dict(momentum_mode="off"),
    "filter":         dict(momentum_mode="filter"),
    "confirm_t04":    dict(momentum_mode="confirm", momentum_window_min=15, momentum_threshold=0.004),
    "confirm_t10":    dict(momentum_mode="confirm", momentum_window_min=15, momentum_threshold=0.010),
    "confirm_t15":    dict(momentum_mode="confirm", momentum_window_min=15, momentum_threshold=0.015),
    "confirm_t10_w5": dict(momentum_mode="confirm", momentum_window_min=5, momentum_threshold=0.010),
}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True, choices=list(CONFIGS))
    ap.add_argument("--symbols", default="513100.SH,518880.SH",
                    help="逗号分隔标的，默认 513100,518880")
    args = ap.parse_args()
    ov = CONFIGS[args.cfg]
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    with build_context("paper") as ctx:
        # 真实数据护栏：回测绝不允许 mock 源（与 cli.py 同铁律）
        providers = list(getattr(ctx.hub, "providers", {}).keys())
        if "mock" in providers:
            print("REJECTED: 检测到 MockProvider，拒绝回测（真实数据铁律）")
            return
        cfg = ETFT0Config(symbols=symbols, **ov)
        bt = ETFT0Backtester(ctx.settings, ctx.hub, initial_cash=CASH, config=cfg)
        res = bt.run(START, END)
        m = res.metrics or {}
        rec = {
            "cfg": args.cfg, "symbols": symbols, "start": str(START), "end": str(END),
            "providers": providers,
            "total_return": m.get("total_return"), "max_dd": m.get("max_drawdown"),
            "t0_pnl": m.get("t0_pnl"), "t0_legs": m.get("t0_legs"),
            "t0_win": m.get("t0_win_legs"), "t0_loss": m.get("t0_loss_legs"),
            "n_trades": m.get("n_trades"), "n_days": m.get("n_days"),
            "minute_available": m.get("minute_available"),
        }
        line = json.dumps(rec, ensure_ascii=False)
        print(line)
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(line + "\n")

if __name__ == "__main__":
    main()
