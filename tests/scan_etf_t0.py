"""ETF T+0 参数扫描：3 个月 QMT 真实数据（2026-05-18 ~ 08-18，63 交易日）。

单因素扫描（base 各参数逐个变档）→ 组合验证。
输出做T净盈亏(t0_pnl)、腿数、胜率、组合区间收益、最大回撤。

用法：python scripts/scan_etf_t0.py --group {base,sell,grid,stop,slice,trades,interval,combos,all}
结果追加写入 logs/etf_t0_scan.txt。
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
                   "logs", "etf_t0_scan.txt")

BASE = dict(
    sell_dev_threshold=0.008,
    buy_dev_threshold=0.008,
    grid_step=0.005,
    stop_pct=0.005,
    t_slice_ratio=0.3,
    max_trades_per_symbol_per_day=2,
    min_interval_minutes=5,
    same_day_roundtrip=False,
)

# 单因素档位：key -> [(覆盖值, 标签)]
SWEEPS = [
    ("sell_dev_threshold", "sell", [(0.006, "0.6%"), (0.010, "1.0%"), (0.012, "1.2%"), (0.015, "1.5%")]),
    ("grid_step",          "grid", [(0.003, "0.3%"), (0.008, "0.8%"), (0.010, "1.0%")]),
    ("stop_pct",           "stop", [(0.003, "0.3%"), (0.008, "0.8%"), (0.010, "1.0%")]),
    ("t_slice_ratio",      "slice", [(0.2, "20%"), (0.4, "40%"), (0.5, "50%")]),
    ("max_trades_per_symbol_per_day", "trades", [(1, "1"), (3, "3"), (4, "4")]),
    ("min_interval_minutes", "interval", [(10, "10m"), (15, "15m")]),
]

def run_bt(ctx, overrides: dict) -> dict:
    cfg = ETFT0Config(**{**BASE, **overrides})
    bt = ETFT0Backtester(ctx.settings, ctx.hub, initial_cash=CASH, config=cfg)
    res = bt.run(START, END)
    m = res.metrics or {}
    return {
        "total_return": m.get("total_return"),
        "max_dd": m.get("max_drawdown"),
        "t0_pnl": m.get("t0_pnl"),
        "t0_legs": m.get("t0_legs"),
        "t0_win": m.get("t0_win_legs"),
        "t0_loss": m.get("t0_loss_legs"),
        "n_trades": m.get("n_trades"),
    }

def fmt(v, pct=True, nd=3):
    if v is None:
        return "n/a"
    if pct and isinstance(v, (int, float)):
        return f"{v*100:+.{nd}f}%"
    return f"{v:,.2f}"

def row(label, r, buf):
    wr = r["t0_win"] / max(r["t0_legs"], 1)
    line = (f"{label:<30}{fmt(r['total_return'],nd=2):>9}{fmt(r['max_dd'],nd=2):>9}"
            f"{fmt(r['t0_pnl'],pct=False):>10}{r['t0_legs']:>5}"
            f"{r['t0_win']:>4}/{r['t0_loss']:<4}{fmt(wr,nd=1):>8}{r['n_trades']:>6}")
    print(line)
    buf.append(line)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="all", help="base/sell/grid/stop/slice/trades/interval/combos/all")
    args = ap.parse_args()
    g = args.group
    os.makedirs(os.path.dirname(OUT), exist_ok=True)

    with build_context("paper") as ctx:
        with open(OUT, "a", encoding="utf-8") as f:
            def emit(title):
                print("\n" + title)
                f.write(f"\n===== {title} =====\n")
                head = (f"{'参数':<30}{'区间收益':>9}{'最大回撤':>9}{'T0净盈亏¥':>10}{'T0腿':>5}"
                        f"{'胜/负':>9}{'T0胜率':>8}{'总成交':>6}")
                print(head); f.write(head + "\n")
            buf = []
            base_r = run_bt(ctx, {})
            if g in ("base", "all"):
                emit("BASE（当前默认）")
                row("BASE", base_r, buf)
                for line in buf: f.write(line + "\n")
            for key, gname, vals in SWEEPS:
                if g not in (gname, "all"):
                    continue
                buf = []
                emit(f"单因素 {key}（每档 = 其余参数用 BASE）")
                for v, label in vals:
                    r = run_bt(ctx, {key: v})
                    row(f"  {key}={label}", r, buf)
                for line in buf: f.write(line + "\n")
            if g in ("combos", "all"):
                buf = []
                emit("组合验证")
                combos = [
                    ("A: sell0.6% + grid0.3% + stop0.8%", dict(sell_dev_threshold=0.006, grid_step=0.003, stop_pct=0.008)),
                    ("B: A + trades3 + interval15m + slice20%", dict(sell_dev_threshold=0.006, grid_step=0.003, stop_pct=0.008,
                                                                      max_trades_per_symbol_per_day=3, min_interval_minutes=15,
                                                                      t_slice_ratio=0.2)),
                    ("C: B 但 sell1.0%", dict(sell_dev_threshold=0.010, grid_step=0.003, stop_pct=0.008,
                                               max_trades_per_symbol_per_day=3, min_interval_minutes=15, t_slice_ratio=0.2)),
                    ("D: B + 真T+0双向", dict(sell_dev_threshold=0.006, grid_step=0.003, stop_pct=0.008,
                                               max_trades_per_symbol_per_day=3, min_interval_minutes=15, t_slice_ratio=0.2,
                                               same_day_roundtrip=True)),
                ]
                for label, ov in combos:
                    r = run_bt(ctx, ov)
                    row(label, r, buf)
                for line in buf: f.write(line + "\n")
            print(f"\n[已追加到 {OUT}]")

if __name__ == "__main__":
    main()

