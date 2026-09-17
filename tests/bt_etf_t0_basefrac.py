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
            stop_pct=0.005, sell_dev=0.008, close_dev=0.002, label=None):
    cfg = ETFT0Config(
        symbols=symbols,
        base_fraction=frac,
        base_fraction_override={s: frac for s in symbols},
        # 其余沿用 config/strategies/etf_t0.yaml 的当前值
        t_slice_ratio=0.3, same_day_roundtrip=False,
        # 入场阈值：越高越苛刻 → 腿越少但每条偏离越大
        sell_dev_threshold=sell_dev, buy_dev_threshold=sell_dev,
        # 回归 VWAP 的平腿阈值
        close_leg_dev=close_dev, grid_step=grid_step, stop_pct=stop_pct,
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
        "sell_dev": sell_dev,
        "close_dev": close_dev,
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
    # 入场阈值：dv >= sell_dev_threshold 才开腿。现值 0.008 太苛刻，
    # 1 年 242 个交易日只有 17 天开过 T —— 腿太少是 T0 贡献小的根本原因。
    ap.add_argument("--sell-devs", default="0.008")
    # 回归 VWAP 的平腿阈值
    ap.add_argument("--close-devs", default="0.002")
    # 离线模式：后端运行时会独占 data/db/qmt.duckdb（DuckDB 单写连接），
    # 默认路径读配置会被 PermissionError 挡住。此模式从 config/ 的**副本**加载
    # Settings，绕开 read_active()（Settings.load 仅当路径==DEFAULT_SETTINGS 才查库）。
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    fracs = [float(x) for x in args.fracs.split(",") if x.strip()]
    grid_steps = [float(x) for x in args.grid_steps.split(",") if x.strip()]
    stop_pcts = [float(x) for x in args.stop_pcts.split(",") if x.strip()]
    sell_devs = [float(x) for x in args.sell_devs.split(",") if x.strip()]
    close_devs = [float(x) for x in args.close_devs.split(",") if x.strip()]
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start = datetime.date.fromisoformat(args.start)
    end = datetime.date.fromisoformat(args.end)

    results = []
    if args.offline:
        # 复制一份配置到临时目录再加载：
        #   1) 路径 != DEFAULT_SETTINGS → Settings.load 跳过 read_active()，不碰 DuckDB；
        #   2) 连 config/strategies/ 一起复制 → 策略配置照常解析，且任何迁移写盘
        #      只落在副本上，不会污染真实配置。
        import shutil
        from qmt_trade.core.config import Settings
        # 行情缓存也走同一个 DuckDB（schema="market"），而它被后端独占 →
        # 用 QMT_RUNTIME_DB 把本次进程的库指到独立文件，彻底不碰后端持有的那份。
        # 代价：新库 market schema 为空，首次要重新拉行情，之后就缓存住。
        os.environ["QMT_RUNTIME_DB"] = os.path.join(ROOT, "data", "db", "qmt_bt.duckdb")
        tmpdir = os.path.join(ROOT, "logs", "_bt_offline_cfg")
        os.makedirs(tmpdir, exist_ok=True)
        shutil.copy2(os.path.join(ROOT, "config", "settings.yaml"),
                     os.path.join(tmpdir, "settings.yaml"))
        shutil.copytree(os.path.join(ROOT, "config", "strategies"),
                        os.path.join(tmpdir, "strategies"), dirs_exist_ok=True)
        s = Settings.load(os.path.join(tmpdir, "settings.yaml"))
        print("[offline] 已绕开 DuckDB 加载配置副本；"
              "注意：数据库里已发布的配置覆盖（若有）不生效，本次只看 YAML 值")
        ctx = build_context("paper", settings=s)
    else:
        ctx = build_context("paper")
    with ctx:
        providers = list(getattr(ctx.hub, "providers", {}).keys())
        if "mock" in providers:
            print("REJECTED: 检测到 MockProvider，拒绝回测（真实数据铁律）")
            return
        print(f"数据源: {providers}  区间: {start} ~ {end}  初始资金: {args.cash:,.0f}")
        print(f"标的: {symbols}\n")
        for sd in sell_devs:
            for cd in close_devs:
                for gs in grid_steps:
                    for sp in stop_pcts:
                        for f in fracs:
                            # 只在「该维度有多个取值」时才把取值写进标签，避免标签噪音
                            label = f"{f:.0%}"
                            if len(sell_devs) > 1:
                                label += f"/入场{sd:g}"
                            if len(close_devs) > 1:
                                label += f"/回归{cd:g}"
                            if len(grid_steps) > 1:
                                label += f"/g{gs:g}"
                            if len(stop_pcts) > 1:
                                label += f"/s{sp:g}"
                            print(f"--- 回测 base={f:.0%} 入场={sd:g} 回归={cd:g} "
                                  f"grid={gs:g} stop={sp:g} ...", flush=True)
                            r = run_one(ctx, f, symbols, start, end, args.cash,
                                        grid_step=gs, stop_pct=sp, sell_dev=sd,
                                        close_dev=cd, label=label)
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
