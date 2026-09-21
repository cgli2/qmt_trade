"""趋势突破（trend_breakout）回测：多窗口 + 可选参数敏感性。

用法：
    python tests/bt_trend_breakout_1y.py                       # 默认：基准 × 三窗口
    python tests/bt_trend_breakout_1y.py --set sens --windows 1y   # 参数敏感性
    python tests/bt_trend_breakout_1y.py --smoke               # 小样本冒烟（300 只 × 3 个月）
    python tests/bt_trend_breakout_1y.py --windows 1y --start 2024-09-19 --end 2025-09-18

注意：回测进程需要独占 data/db/qmt.duckdb，运行前请停止后端服务（端口 7099）与看门狗。
结果：控制台表格 + .verify_tmp/trend_breakout_<ts>.json + logs/trend_breakout_1y.txt
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.WARNING)

from qmt_trade.app import build_context                      # noqa: E402
from qmt_trade.core.config import Settings                   # noqa: E402
from qmt_trade.strategies.trend_breakout import (            # noqa: E402
    TrendBreakoutBacktester, TrendBreakoutConfig)

CASH = 1_000_000.0

WINDOWS = {
    "1y":     ("2025-09-19", "2026-09-18", "近一年"),
    "2y":     ("2024-09-19", "2026-09-18", "近两年"),
    "choppy": ("2025-08-15", "2026-08-14", "震荡段"),
    "oos":    ("2023-09-19", "2024-09-18", "样本外熊市"),
}

# 基准配置（与 config/strategies/trend_breakout.yaml 一致）
BASE = dict(
    n1=60, n2=10, n3=40, zf_low=30.0, zf_high=100.0,
    ma_fast=10, ma_mid=20, ma_slow=50, range_max=1.25,
    vol_mult=1.2, vol_mode="amount", breakout_mult=0.98,
    hhv_include_today=False,
    market_ma_days=60, market_filter_enabled=True,
    take_profit1=0.20, take_profit2=0.35, tp1_sell_ratio=0.5,
    stop_pct=0.08, stop_low_mult=0.98, max_hold_days=20,
    ma_exit_enabled=False,
    trail_enabled=False, trail_activate_pct=0.10, trail_drawdown_pct=0.08,
    max_positions=4, position_fraction=0.25, cash_usage_ratio=0.95,
    rank_by="vol_ratio", min_list_days=120,
)

VARIANTS = [
    ("基准 amount口径", {}),
    ("原式字面 share+含当日", {"vol_mode": "share", "hhv_include_today": True}),
    ("原式量纲 share", {"vol_mode": "share"}),
    ("HHV含当日", {"hhv_include_today": True}),
    ("纯量口径 vol", {"vol_mode": "vol"}),
    ("放量1.5倍", {"vol_mult": 1.5}),
    ("放量2.0倍", {"vol_mult": 2.0}),
    ("振幅<1.20", {"range_max": 1.20}),
    ("振幅<1.35", {"range_max": 1.35}),
    ("涨幅20~100", {"zf_low": 20.0}),
    ("涨幅30~150", {"zf_high": 150.0}),
    ("盘整窗20日", {"n3": 20}),
    ("盘整窗60日", {"n3": 60}),
    ("持仓10日", {"max_hold_days": 10}),
    ("持仓30日", {"max_hold_days": 30}),
    ("MA10破位离场", {"ma_exit_enabled": True}),
    ("移动止盈10/8", {"trail_enabled": True}),
    ("3只×33%", {"max_positions": 3, "position_fraction": 0.33}),
    ("5只×20%", {"max_positions": 5, "position_fraction": 0.20}),
    ("弱市闸门关闭", {"market_filter_enabled": False}),
    # —— 组合（敏感性里两个正向方向的叠加，需在三窗口验证是否稳健）——
    ("组合A 振幅1.2+放量1.5", {"range_max": 1.20, "vol_mult": 1.5}),
    ("组合B A+持仓30日", {"range_max": 1.20, "vol_mult": 1.5, "max_hold_days": 30}),
    ("组合C A+5只20%", {"range_max": 1.20, "vol_mult": 1.5, "max_positions": 5,
                        "position_fraction": 0.20}),
    # —— 止损口径改造（2026-09-21 迭代：结构性改动，不是阈值微调）——
    ("止损ATR×2", {"stop_mode": "atr", "atr_mult": 2.0}),
    ("止损ATR×2+最短5日", {"stop_mode": "atr", "atr_mult": 2.0, "min_hold_days": 5}),
    ("止损ATR×3+最短5日", {"stop_mode": "atr", "atr_mult": 3.0, "min_hold_days": 5}),
    ("止损盘整下沿", {"stop_mode": "low"}),
    ("止损盘整下沿+最短5日", {"stop_mode": "low", "min_hold_days": 5}),
    ("原止损+最短5日", {"min_hold_days": 5}),
    # —— 降换手（2026-09-21 第三轮：成本吃掉近两年毛收益的 2/3）——
    ("降换手 3只×33%", {"max_positions": 3, "position_fraction": 0.33}),
    ("降换手 2只×50%", {"max_positions": 2, "position_fraction": 0.50}),
    ("降换手 持仓40日", {"max_hold_days": 40}),
    ("降换手 持仓60日", {"max_hold_days": 60}),
    ("降换手 3只+持仓40日", {"max_positions": 3, "position_fraction": 0.33,
                             "max_hold_days": 40}),
    # —— 换入场结构（第四轮：突破当天追 → 突破后回踩确认）——
    ("入场回踩确认", {"entry_mode": "pullback"}),
    ("回踩确认3日窗口", {"entry_mode": "pullback", "pullback_window": 3}),
    ("回踩确认缩量0.4", {"entry_mode": "pullback", "pullback_vol_shrink": 0.4}),
    ("回踩确认带5%", {"entry_mode": "pullback", "pullback_band": 0.05}),
]


def run_one(ctx, ov: dict, start, end, cash: float, symbols=None) -> dict:
    cfg = TrendBreakoutConfig(**{**BASE, **ov})
    bt = TrendBreakoutBacktester(ctx.settings, ctx.hub, initial_cash=cash, config=cfg)
    if symbols is not None:
        bt._universe = lambda _d, _s=symbols: list(_s)      # noqa: SLF001 - 冒烟限池
    t0 = time.time()
    res = bt.run(start, end)
    elapsed = time.time() - t0
    m, c = res.metrics or {}, res.cost_attribution or {}
    ct = res.closed_trades or []
    pnls = [float(t.get("pnl") or 0.0) for t in ct]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    holds = [int(t.get("holding_days") or 0) for t in ct]
    panel = getattr(bt, "_panel", None)
    sig_total = int(panel["signal"].sum()) if panel is not None and not panel.empty else 0
    return {
        "total_return": m.get("total_return"), "cagr": m.get("cagr"),
        "sharpe": m.get("sharpe"), "max_dd": m.get("max_drawdown"),
        "win_rate": m.get("win_rate"), "n_days": m.get("n_days"),
        "n_trades": m.get("n_trades"), "n_closed": len(ct),
        "gross": (c.get("gross_pnl") or 0) / cash, "cost": (c.get("cost_drag") or 0) / cash,
        "net": (c.get("net_pnl") or 0) / cash, "turnover": c.get("single_side_turnover"),
        "avg_win": avg_win, "avg_loss": avg_loss,
        "payoff": (avg_win / avg_loss) if avg_loss > 0 else None,
        "avg_hold": (sum(holds) / len(holds)) if holds else 0.0,
        "n_signal_rows": sig_total, "n_buys": len(getattr(bt, "signal_log", [])),
        "elapsed_s": round(elapsed, 1),
        "signal_log": list(getattr(bt, "signal_log", [])),
        "closed_trades": ct,
    }


def disk_universe() -> list[str]:
    """行情源全部熔断时的兜底：直接从 DuckDB 市场库取已缓存的标的列表。

    QMT 客户端关闭 + akshare 熔断时 ``hub.get_instruments()`` 拿不到标的池，
    但历史日线仍在磁盘缓存里，回测历史窗口不需要联网。
    """
    try:
        import duckdb
        con = duckdb.connect(str(ROOT / "data" / "db" / "qmt.duckdb"), read_only=True)
        try:
            tabs = con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='market' AND table_name LIKE 'dataset%' "
                "AND table_name <> 'dataset_coverage'").fetchall()
            out: set[str] = set()
            for (t,) in tabs:
                try:
                    rows = con.execute(f'SELECT DISTINCT symbol FROM market."{t}"').fetchall()
                    out.update(str(r[0]) for r in rows if r and r[0])
                except Exception:                              # noqa: BLE001
                    continue
            return sorted(out)
        finally:
            con.close()
    except Exception as exc:                                  # noqa: BLE001
        print("磁盘标的兜底失败:", exc, flush=True)
        return []


def bench_index(ctx, start, end) -> dict:
    """沪深300 同期买入持有（基准对比）。"""
    try:
        idx = ctx.hub.get_index_bars("000300.SH", start, end)
        if idx is None or idx.empty:
            return {}
        cl = [float(x) for x in idx.sort_values("date")["close"]]
        if len(cl) < 2:
            return {}
        peak, mdd = cl[0], 0.0
        for x in cl:
            peak = max(peak, x)
            mdd = min(mdd, x / peak - 1.0)
        return {"bench_return": cl[-1] / cl[0] - 1.0, "bench_max_dd": mdd,
                "bench_days": len(cl)}
    except Exception as exc:                                  # noqa: BLE001
        return {"bench_error": str(exc)}


def fmt(v):
    return "n/a" if v is None else f"{v:+.2%}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="1y,2y,choppy",
                    help="逗号分隔：1y / 2y / choppy")
    ap.add_argument("--set", default="base", choices=["base", "sens"],
                    help="base=只跑基准；sens=跑全部敏感性变体")
    ap.add_argument("--start", default="", help="覆盖窗口起日（单窗口时用）")
    ap.add_argument("--end", default="", help="覆盖窗口止日（单窗口时用）")
    ap.add_argument("--cash", type=float, default=CASH)
    ap.add_argument("--smoke", action="store_true", help="小样本冒烟：300 只 × 3 个月")
    ap.add_argument("--only", default="", help="只跑指定变体（逗号分隔名称）")
    ap.add_argument("--keep-log", action="store_true", help="存档保留逐笔 signal_log/closed_trades")
    args = ap.parse_args()

    wkeys = [w.strip() for w in args.windows.split(",") if w.strip() in WINDOWS] or ["1y"]
    variants = VARIANTS if args.set == "sens" else VARIANTS[:1]
    if args.only:
        names = {n.strip() for n in args.only.split(",") if n.strip()}
        variants = [v for v in VARIANTS if v[0] in names] or VARIANTS[:1]

    settings = Settings.load(str(ROOT / "config" / "settings.yaml"))
    out_dir = ROOT / ".verify_tmp"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rows = []

    def _dump(tag: str = ""):
        payload = {"cash": args.cash, "smoke": args.smoke, "set": args.set, "rows": rows}
        p = out_dir / f"trend_breakout_{stamp}{tag}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                     encoding="utf-8")
        return p
    with build_context("paper", settings=settings) as ctx:
        providers = list(getattr(ctx.hub, "providers", {}).keys())
        if "mock" in providers:
            print("REJECTED: 检测到 MockProvider，拒绝回测（真实数据铁律）")
            return 1
        print(f"providers={providers}", flush=True)

        pool_ok = True
        try:
            infos = ctx.hub.get_instruments()
            allsym = list(infos.keys()) if isinstance(infos, dict) else \
                [getattr(i, "symbol", "") for i in (infos or [])]
            allsym = [s for s in allsym if s]
            if not allsym:
                raise ValueError("标的池为空（行情源可能已熔断）")
        except Exception as exc:                              # noqa: BLE001
            print(f"标的池获取失败（{exc}），改用磁盘缓存标的列表", flush=True)
            allsym = disk_universe()
            pool_ok = False
            print("磁盘标的数:", len(allsym), flush=True)

        symbols = None
        if args.smoke:
            symbols = allsym[:300]
            wkeys = wkeys[:1]
        elif not pool_ok:
            symbols = allsym                                  # 断源时显式传入标的池

        for wkey in wkeys:
            ws, we, wdesc = WINDOWS[wkey]
            start = datetime.date.fromisoformat(args.start or ws)
            end = datetime.date.fromisoformat(args.end or we)
            bench = bench_index(ctx, start, end)
            print(f"\n===== 窗口 {wkey}（{wdesc}） {start} ~ {end} =====", flush=True)
            print(f"  基准 沪深300: 收益 {fmt(bench.get('bench_return'))} "
                  f"最大回撤 {fmt(bench.get('bench_max_dd'))}", flush=True)
            for name, ov in variants:
                try:
                    r = run_one(ctx, ov, start, end, args.cash, symbols)
                    row = {"window": wkey, "name": name, "start": start.isoformat(),
                           "end": end.isoformat(), "bench": bench,
                           **{k: v for k, v in r.items()
                              if k not in ("signal_log", "closed_trades")}}
                    if args.keep_log:
                        row["signal_log"] = r["signal_log"]
                        row["closed_trades"] = [
                            {k: (v.isoformat() if isinstance(v, (datetime.date, datetime.datetime))
                                 else v) for k, v in t.items()}
                            for t in r["closed_trades"]]
                    rows.append(row)
                    print(f"  {name:<20} ret={fmt(r['total_return'])} "
                          f"sharpe={'n/a' if r['sharpe'] is None else round(r['sharpe'], 2)} "
                          f"mdd={fmt(r['max_dd'])} win={r['win_rate']:.1%} "
                          f"net={fmt(r['net'])} gross={fmt(r['gross'])} "
                          f"cost={fmt(r['cost'])} 换手={(r['turnover'] or 0):.1f}x "
                          f"平仓={r['n_closed']} 均持={r['avg_hold']:.1f}d "
                          f"盈亏比={(r['payoff'] or 0):.2f} 信号={r['n_signal_rows']} "
                          f"({r['elapsed_s']}s)", flush=True)
                except Exception as exc:                      # noqa: BLE001
                    print(f"  {name:<20} ERROR: {type(exc).__name__}: {exc}", flush=True)
                    rows.append({"window": wkey, "name": name,
                                 "start": start.isoformat(), "end": end.isoformat(),
                                 "error": f"{type(exc).__name__}: {exc}"})
            # 每跑完一个窗口立即落盘（长任务防丢）
            print(f"  [存档] {_dump('_' + wkey)}", flush=True)

    out = _dump()
    # default=str：closed_trades/signal_log 里含 date 对象
    log = ROOT / "logs" / "trend_breakout_1y.txt"
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"cash": args.cash, "smoke": args.smoke, "set": args.set,
                            "rows": rows}, ensure_ascii=False, default=str) + "\n")
    print(f"\n已存档：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
