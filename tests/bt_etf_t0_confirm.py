"""顺势方向检验：momentum_mode=confirm（强动量才开腿）完整扫描（2026-05-18~08-18）。

分组：base / window / threshold / combos
用法：python scripts/bt_etf_t0_confirm.py --group <group>
结果追加 logs/etf_t0_confirm.txt
"""
import sys, os, logging, datetime, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.WARNING)

from qmt_trade.app import build_context
from qmt_trade.strategies.etf_t0 import ETFT0Backtester, ETFT0Config

START, END = datetime.date(2026, 5, 18), datetime.date(2026, 8, 18)
CASH = 1_000_000.0
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "logs", "etf_t0_confirm.txt")

BASE = dict(
    sell_dev_threshold=0.008, buy_dev_threshold=0.008,
    grid_step=0.005, stop_pct=0.005, t_slice_ratio=0.3,
    max_trades_per_symbol_per_day=2, min_interval_minutes=5,
    same_day_roundtrip=False, momentum_mode="confirm",
    momentum_window_min=15, momentum_threshold=0.004,
)

def run_bt(ctx, overrides: dict) -> dict:
    cfg = ETFT0Config(**{**BASE, **overrides})
    bt = ETFT0Backtester(ctx.settings, ctx.hub, initial_cash=CASH, config=cfg)
    res = bt.run(START, END)
    m = res.metrics or {}
    return {"total_return": m.get("total_return"), "max_dd": m.get("max_drawdown"),
            "t0_pnl": m.get("t0_pnl"), "t0_legs": m.get("t0_legs"),
            "t0_win": m.get("t0_win_legs"), "t0_loss": m.get("t0_loss_legs"),
            "n_trades": m.get("n_trades")}

def fmt(v, pct=True, nd=2):
    if v is None:
        return "n/a"
    if pct and isinstance(v, (int, float)):
        return f"{v*100:+.{nd}f}%"
    return f"{v:,.2f}"

def row(label, r, buf):
    wr = r["t0_win"] / max(r["t0_legs"], 1)
    line = (f"{label:<34}{fmt(r['total_return']):>9}{fmt(r['max_dd']):>9}"
            f"{fmt(r['t0_pnl'],pct=False):>11}{r['t0_legs']:>5}"
            f"{r['t0_win']:>4}/{r['t0_loss']:<4}{fmt(wr):>8}{r['n_trades']:>6}")
    print(line); buf.append(line)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="base", help="base/window/threshold/combos")
    args = ap.parse_args()
    g = args.group
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with build_context("paper") as ctx:
        with open(OUT, "a", encoding="utf-8") as f:
            buf = []
            def emit(title):
                print("\n" + title); f.write(f"\n===== {title} =====\n")
                head = (f"{'参数':<34}{'区间收益':>9}{'最大回撤':>9}{'T0净盈亏¥':>11}{'T0腿':>5}"
                        f"{'胜/负':>9}{'T0胜率':>8}{'总成交':>6}")
                print(head); f.write(head + "\n")
            if g == "base":
                emit("三模式基线对比（其余参数默认）")
                for mode, lab in (("off", "off（无动量）"), ("filter", "filter（逆势过滤）"),
                                  ("confirm", "confirm（顺势确认）")):
                    row(lab, run_bt(ctx, {"momentum_mode": mode}), buf)
            elif g == "window":
                emit("confirm 窗口微调（threshold=0.4% 固定）")
                for w in (5, 30, 60, 120):
                    row(f"confirm w={w}m", run_bt(ctx, {"momentum_window_min": w}), buf)
            elif g == "threshold":
                emit("confirm 阈值微调（window=15 固定）")
                for t, lab in ((0.002, "0.2%"), (0.006, "0.6%"), (0.010, "1.0%"), (0.015, "1.5%")):
                    row(f"confirm t={lab}", run_bt(ctx, {"momentum_threshold": t}), buf)
            elif g == "combos":
                emit("confirm 组合验证（顺势 + 之前表现好的参数）")
                combos = [
                    ("confirm + stop0.8%+grid0.3%", dict(stop_pct=0.008, grid_step=0.003)),
                    ("confirm + stop0.8%+grid0.3%+trades3+15m", dict(stop_pct=0.008, grid_step=0.003,
                                                                      max_trades_per_symbol_per_day=3,
                                                                      min_interval_minutes=15)),
                    ("confirm 双向", dict(same_day_roundtrip=True)),
                    ("confirm 双向 + stop0.8%+grid0.3%", dict(same_day_roundtrip=True, stop_pct=0.008,
                                                              grid_step=0.003)),
                    ("confirm w=5m + t=1.0% + stop0.8%+grid0.3%", dict(momentum_window_min=5,
                                                                        momentum_threshold=0.010,
                                                                        stop_pct=0.008, grid_step=0.003)),
                ]
                for label, ov in combos:
                    row(label, run_bt(ctx, ov), buf)
            for line in buf:
                f.write(line + "\n")
            print(f"\n[已追加 {OUT}]")

if __name__ == "__main__":
    main()
