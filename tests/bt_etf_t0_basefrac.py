"""ETF T+0 底仓占比对比回测：12% vs 20%（及其他任意档位）。

用法：
    python tests/bt_etf_t0_basefrac.py                      # 默认 12% vs 20%
    python tests/bt_etf_t0_basefrac.py --fracs 0.12,0.20,0.30
    python tests/bt_etf_t0_basefrac.py --start 2025-08-18 --end 2026-08-18

背景（2026-09-17）：单票上限从 12% 放宽到 20% 后，需要量化底仓规模对最终业绩的影响。
注意 base_fraction_override 此前是死配置（声明后无引用），现已由
``ETFT0Config.base_fraction_for()`` 在回测与实盘统一生效，本脚本才能真实对比。
"""
import sys, os, logging, datetime, argparse, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.WARNING)

from qmt_trade.app import build_context
from qmt_trade.strategies.etf_t0 import ETFT0Backtester, ETFT0Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "logs", "etf_t0_basefrac.txt")


def run_one(ctx, frac, symbols, start, end, cash, grid_step=0.003,
            stop_pct=0.005, label=None):
    cfg = ETFT0Config(
        symbols=symbols,
        base_fraction=frac,
        base_fraction_override={s: frac for s in symbols},
        # 其余沿用 config/strategies/etf_t0.yaml 的当前值
        t_slice_ratio=0.3, same_day_roundtrip=False,
        sell_dev_threshold=0.008, buy_dev_threshold=0.008,
        close_leg_dev=0.002, grid_step=grid_step, stop_pct=stop_pct,
        max_trades_per_symbol_per_day=2, min_interval_minutes=5,
        open_t_start="09:35", open_t_end="14:30", force_flat_time="14:50",
        max_daily_loss_pct=0.003, buyback_max_notional_ratio=0.03,
        min_minutes_per_day=5, momentum_mode="filter",
        momentum_window_min=15, momentum_threshold=0.004,
    )
    bt = ETFT0Backtester(ctx.settings, ctx.hub, initial_cash=cash, config=cfg)
    res = bt.run(start, end)
    m = res.metrics or {}
    return {
        "frac": frac,
        "grid_step": grid_step,
        "stop_pct": stop_pct,
        "label": label or f"{frac:.0%}",
        "total_return": m.get("total_return"),
        "max_drawdown": m.get("max_drawdown"),
        "sharpe": m.get("sharpe"),
        "t0_pnl": m.get("t0_pnl"),
        "t0_legs": m.get("t0_legs"),
        "t0_win": m.get("t0_win_legs"),
        "t0_loss": m.get("t0_loss_legs"),
        "t0_by_tag": m.get("t0_by_tag"),
        "n_trades": m.get("n_trades"),
        "n_days": m.get("n_days"),
        "minute_available": m.get("minute_available"),
        "final_equity": res.equity_curve[-1] if res.equity_curve else None,
        "open_positions": res.open_positions,
        "details": res.details,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fracs", default="0.12,0.20")
    ap.add_argument("--symbols", default="513100.SH,159941.SZ")
    ap.add_argument("--start", default="2025-08-18")
    ap.add_argument("--end", default="2026-08-18")
    ap.add_argument("--cash", type=float, default=1_000_000.0)
    # grid_step 敏感性：网格越窄越早落袋，避免腿漂成反向止损
    ap.add_argument("--grid-steps", default="0.003")
    # stop_pct 敏感性：反向止损距离，是仅次于止损腿数的第二大漏点
    ap.add_argument("--stop-pcts", default="0.005")
    args = ap.parse_args()

    fracs = [float(x) for x in args.fracs.split(",") if x.strip()]
    grid_steps = [float(x) for x in args.grid_steps.split(",") if x.strip()]
    stop_pcts = [float(x) for x in args.stop_pcts.split(",") if x.strip()]
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start = datetime.date.fromisoformat(args.start)
    end = datetime.date.fromisoformat(args.end)

    results = []
    with build_context("paper") as ctx:
        providers = list(getattr(ctx.hub, "providers", {}).keys())
        if "mock" in providers:
            print("REJECTED: 检测到 MockProvider，拒绝回测（真实数据铁律）")
            return
        print(f"数据源: {providers}  区间: {start} ~ {end}  初始资金: {args.cash:,.0f}")
        print(f"标的: {symbols}\n")
        for gs in grid_steps:
            for sp in stop_pcts:
                for f in fracs:
                    # 只在「该维度有多个取值」时才把取值写进标签，避免标签噪音
                    label = f"{f:.0%}"
                    if len(grid_steps) > 1:
                        label += f"/g{gs:g}"
                    if len(stop_pcts) > 1:
                        label += f"/s{sp:g}"
                    print(f"--- 回测 base_fraction={f:.0%} grid_step={gs:g} "
                          f"stop_pct={sp:g} ...", flush=True)
                    r = run_one(ctx, f, symbols, start, end, args.cash,
                                grid_step=gs, stop_pct=sp, label=label)
                r["providers"] = providers
                r["start"], r["end"], r["cash"] = str(start), str(end), args.cash
                results.append(r)

    print("\n" + "=" * 96)
    print(f"{'档位':>14} | {'总收益':>9} {'最大回撤':>9} {'夏普':>7} | "
          f"{'T0净盈亏':>10} {'腿数':>5} {'胜':>4} {'负':>4} | {'期末权益':>12}")
    print("-" * 96)
    def pct(v):
        return f"{v:.2%}" if isinstance(v, (int, float)) else "-"

    def num(v):
        return f"{v:,.0f}" if isinstance(v, (int, float)) else "-"

    def shp(v):
        return f"{v:.2f}" if isinstance(v, (int, float)) else "-"

    for r in results:
        print(f"{r['label']:>14} | {pct(r['total_return']):>9} {pct(r['max_drawdown']):>9} "
              f"{shp(r['sharpe']):>7} | {num(r['t0_pnl']):>10} "
              f"{num(r['t0_legs']):>5} {num(r['t0_win']):>4} {num(r['t0_loss']):>4} | "
              f"{num(r['final_equity']):>12}")
    print("=" * 96)

    for r in results:
        print(f"\n[{r['label']}] 明细：")
        for d in (r.get("details") or []):
            print(f"   {d}")
        if r.get("t0_by_tag"):
            print(f"   {'平腿原因':<16}{'腿数':>6}{'净盈亏':>12}{'单腿均值':>12}")
            for tag, v in r["t0_by_tag"].items():
                print(f"   {tag:<16}{v['legs']:>6}{v['pnl']:>12,.0f}{v['avg_pnl']:>12,.1f}")
        print(f"   期末持仓: {r.get('open_positions')}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as fh:
        for r in results:
            rec = {k: v for k, v in r.items() if k != "details"}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n结果已追加: {OUT}")


if __name__ == "__main__":
    main()
