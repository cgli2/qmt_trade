"""confirm_t15 分标的归因（一年，真实 QMT 数据）——验证 +21k 是否由 513310 单独贡献。"""
import sys, os, logging, datetime, json, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.WARNING)

from qmt_trade.app import build_context
from qmt_trade.strategies.etf_t0 import ETFT0Backtester, ETFT0Config

START, END = datetime.date(2025, 8, 18), datetime.date(2026, 8, 18)
CASH = 1_000_000.0
OV = dict(momentum_mode="confirm", momentum_window_min=15, momentum_threshold=0.015)

def run(ctx, symbols):
    cfg = ETFT0Config(symbols=symbols, **OV)
    bt = ETFT0Backtester(ctx.settings, ctx.hub, initial_cash=CASH, config=cfg)
    res = bt.run(START, END)
    m = res.metrics or {}
    return {"symbols": symbols, "total_return": m.get("total_return"),
            "max_dd": m.get("max_drawdown"), "t0_pnl": m.get("t0_pnl"),
            "t0_legs": m.get("t0_legs"), "t0_win": m.get("t0_win_legs"),
            "t0_loss": m.get("t0_loss_legs"), "n_trades": m.get("n_trades")}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--syms", default="513100.SH,518880.SH",
                    help="逗号分隔标的组合")
    args = ap.parse_args()
    syms = [s.strip() for s in args.syms.split(",") if s.strip()]
    with build_context("paper") as ctx:
        prov = list(getattr(ctx.hub, "providers", {}).keys())
        assert "mock" not in prov, "拒绝 mock"
        r = run(ctx, syms)
        print(json.dumps(r, ensure_ascii=False))

