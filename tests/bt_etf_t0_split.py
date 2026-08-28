"""分标的动量参数配置 1 年回测验证（2025-08-18 ~ 2026-08-18，真实 QMT 数据）。

A = 分标的：513310 confirm t1.5% / 513100+518880 filter（momentum_override 生效）
B = 全局 filter（对照）
C = 全局 confirm t1.5%（对照）

用法：python scripts/bt_etf_t0_split.py --cfg A|B|C
结果追加 logs/etf_t0_split.txt
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
                   "logs", "etf_t0_split.txt")
SYMS = ["513100.SH", "518880.SH", "513310.SH"]
OVERRIDE = {"513310.SH": {"momentum_mode": "confirm",
                          "momentum_window_min": 15,
                          "momentum_threshold": 0.015}}

CONFIGS = {
    "A": dict(momentum_mode="filter", momentum_override=OVERRIDE),
    "B": dict(momentum_mode="filter", momentum_override={}),
    "C": dict(momentum_mode="confirm", momentum_window_min=15, momentum_threshold=0.015,
              momentum_override={}),
}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True, choices=list(CONFIGS))
    args = ap.parse_args()
    ov = CONFIGS[args.cfg]
    with build_context("paper") as ctx:
        providers = list(getattr(ctx.hub, "providers", {}).keys())
        if "mock" in providers:
            print("REJECTED: 检测到 MockProvider，拒绝回测（真实数据铁律）")
            return
        cfg = ETFT0Config(symbols=SYMS, **ov)
        bt = ETFT0Backtester(ctx.settings, ctx.hub, initial_cash=CASH, config=cfg)
        res = bt.run(START, END)
        m = res.metrics or {}
        rec = {
            "cfg": args.cfg, "symbols": SYMS, "override": ov.get("momentum_override", {}),
            "providers": providers, "minute_available": m.get("minute_available"),
            "total_return": m.get("total_return"), "max_dd": m.get("max_drawdown"),
            "t0_pnl": m.get("t0_pnl"), "t0_legs": m.get("t0_legs"),
            "t0_win": m.get("t0_win_legs"), "t0_loss": m.get("t0_loss_legs"),
            "n_trades": m.get("n_trades"), "n_days": m.get("n_days"),
        }
        line = json.dumps(rec, ensure_ascii=False)
        print(line)
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(line + "\n")

if __name__ == "__main__":
    main()
