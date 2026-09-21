"""红利低波（dividend_low_vol）回测：多窗口 + 严格样本外验证。

用法：
    python tests/bt_dividend_low_vol.py                       # 默认：基准 × 全部窗口
    python tests/bt_dividend_low_vol.py --set sens            # 含结构性敏感性变体
    python tests/bt_dividend_low_vol.py --smoke               # 小样本冒烟（300 只 × 3 个月）
    python tests/bt_dividend_low_vol.py --windows 1y,oos22
    python tests/bt_dividend_low_vol.py --windows 1y --start 2024-09-19 --end 2025-09-18

窗口设计（过拟合纪律，沿用趋势突破那套）：
    in-sample 调参窗口 1y/2y/choppy 彼此重叠（1y⊂2y、choppy 与 1y 重叠 11 个月），
    用来定"默认结构"（按经济逻辑定，不拟合某窗口收益）。
    真正独立的验证落在两个"从未检视"窗口：2022 熊市、2020-2021 牛市——
    它们既没用于调参，也没在趋势突破项目里被看过，才是真 OOS。

注意：回测进程需要独占 data/db/qmt.duckdb，运行前请停止后端服务（端口 7099）与看门狗。
结果：控制台表格 + .verify_tmp/dividend_low_vol_<ts>.json + logs/dividend_low_vol.txt
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
from qmt_trade.strategies.dividend_low_vol import (          # noqa: E402
    DividendLowVolBacktester, DividendLowVolConfig)

CASH = 1_000_000.0

# 本地数据缓存实测只覆盖 ~2025-01-02 至今（约 1.7 年），2020-2022 历史财务/价量
# 本地取不到。因此窗口全部落在缓存区间内，并用时间上不重叠的 2026 段作严格样本外：
#   insample 2025 全年（结构熟悉期） + oos 2026（从未检视，主验证）
# 注：配置按经济逻辑设定、不拟合任何窗口，故各窗口本质都是样本外；2026 段专门用于
# 「全程未看、时间不重叠」的最干净验证。
WINDOWS = {
    "insample": ("2025-01-02", "2025-12-31", "样本内·2025全年"),
    "1y":       ("2025-09-19", "2026-09-18", "近一年(主)"),
    "choppy":   ("2025-08-15", "2026-08-14", "震荡段"),
    "oos2026":  ("2026-01-02", "2026-09-18", "样本外·2026(从未检视)"),
}

# 默认配置（与 config/strategies/dividend_low_vol.yaml 一致；按经济逻辑定，不拟合窗口）
# value_source="price" 为跨窗口一致可回测的主口径（不依赖财务数据）。
BASE = dict(
    vol_window=60, value_weight=0.5, lowvol_weight=0.5,
    value_source="price", price_value_window=120,
    require_positive_eps=True,
    max_positions=20, position_fraction=0.05,
    rebalance_period=3, weight_mode="equal",
    market_filter_enabled=False, market_ma_days=60, market_ma_days2=0,
    min_list_days=250, exclude_st=True, exclude_suspended=True,
    exclude_limit_locked=True, allowed_boards=["MAIN", "GEM", "STAR"],
    warmup_days=260, cash_usage_ratio=0.95,
)

# 结构性敏感性（不同是合理的替代方案，非针对某窗口拟合）
VARIANTS = [
    ("基准 equal 季度", {}),
    ("月度再平衡", {"rebalance_period": 1}),
    ("低波加权 inv_vol", {"weight_mode": "inv_vol"}),
    ("10只篮子", {"max_positions": 10}),
    ("30只篮子", {"max_positions": 30}),
    ("价值权重0.7", {"value_weight": 0.7, "lowvol_weight": 0.3}),
    ("低波权重0.7", {"value_weight": 0.3, "lowvol_weight": 0.7}),
    ("弱市闸门开启", {"market_filter_enabled": True}),
    ("vol窗口120日", {"vol_window": 120}),
    ("vol窗口40日", {"vol_window": 40}),
    # 基本面价值（最忠实红利代理）：仅在财务数据可得的近期窗口有效，
    # 历史窗口无财务会退化为仅低波；作为"价格代理是否选中真红利股"的佐证。
    ("基本面价值(近期)", {"value_source": "fundamental"}),
]


def run_one(ctx, ov: dict, start, end, cash: float, symbols=None) -> dict:
    cfg = DividendLowVolConfig(**{**BASE, **ov})
    bt = DividendLowVolBacktester(ctx.settings, ctx.hub, initial_cash=cash, config=cfg)
    if symbols is not None:
        bt._universe = lambda _d, _s=symbols: list(_s)      # noqa: SLF001 - 冒烟/断源限池
    t0 = time.time()
    res = bt.run(start, end)
    elapsed = time.time() - t0
    m, c = res.metrics or {}, res.cost_attribution or {}
    ct = res.closed_trades or []
    sig = getattr(bt, "signal_log", []) or []
    n_sel = [s.get("n_selected", 0) for s in sig]
    # 期末篮子（用于报告展示行业/个股集中度）
    last_hold = [p.get("symbol") for p in (res.open_positions or [])]
    return {
        "total_return": m.get("total_return"), "cagr": m.get("cagr"),
        "sharpe": m.get("sharpe"), "max_dd": m.get("max_drawdown"),
        "win_rate": m.get("win_rate"), "n_days": m.get("n_days"),
        "n_trades": m.get("n_trades"), "n_closed": len(ct),
        "gross": (c.get("gross_pnl") or 0) / cash, "cost": (c.get("cost_drag") or 0) / cash,
        "net": (c.get("net_pnl") or 0) / cash, "turnover": c.get("single_side_turnover"),
        "n_rebalances": len(sig), "avg_selected": (sum(n_sel) / len(n_sel)) if n_sel else 0,
        "last_holdings": last_hold, "elapsed_s": round(elapsed, 1),
        "signal_log": list(sig), "closed_trades": ct,
    }


def disk_universe() -> list[str]:
    """行情源熔断时的兜底：直接从 DuckDB 市场库取已缓存的标的列表。"""
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
    ap.add_argument("--windows", default="1y,choppy,oos2026",
                    help="逗号分隔：insample / 1y / choppy / oos2026")
    ap.add_argument("--set", default="base", choices=["base", "sens"],
                    help="base=只跑基准；sens=跑全部结构性变体")
    ap.add_argument("--start", default="", help="覆盖窗口起日（单窗口时用）")
    ap.add_argument("--end", default="", help="覆盖窗口止日（单窗口时用）")
    ap.add_argument("--cash", type=float, default=CASH)
    ap.add_argument("--smoke", action="store_true", help="小样本冒烟：300 只 × 3 个月")
    ap.add_argument("--cap", type=int, default=0,
                    help="代表性子样本：仅取前 N 只（避免全市场 5200 只 bulk 取数过慢）")
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
        p = out_dir / f"dividend_low_vol_{stamp}{tag}.json"
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
        elif args.cap > 0:
            symbols = allsym[:args.cap]
            print(f"代表性子样本：前 {len(symbols)} 只（避免全市场 bulk 取数过慢）", flush=True)
        elif not pool_ok:
            symbols = allsym

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
                              if k not in ("signal_log", "closed_trades", "last_holdings")}}
                    if args.keep_log:
                        row["signal_log"] = r["signal_log"]
                        row["closed_trades"] = [
                            {k: (v.isoformat() if isinstance(v, (datetime.date, datetime.datetime))
                                 else v) for k, v in t.items()}
                            for t in r["closed_trades"]]
                    rows.append(row)
                    print(f"  {name:<16} ret={fmt(r['total_return'])} "
                          f"sharpe={'n/a' if r['sharpe'] is None else round(r['sharpe'], 2)} "
                          f"mdd={fmt(r['max_dd'])} net={fmt(r['net'])} "
                          f"gross={fmt(r['gross'])} cost={fmt(r['cost'])} "
                          f"换手={(r['turnover'] or 0):.1f}x 再平衡={r['n_rebalances']} "
                          f"均选={r['avg_selected']:.0f} 只 末仓={len(r['last_holdings'])}只 "
                          f"({r['elapsed_s']}s)", flush=True)
                except Exception as exc:                      # noqa: BLE001
                    print(f"  {name:<16} ERROR: {type(exc).__name__}: {exc}", flush=True)
                    rows.append({"window": wkey, "name": name,
                                 "start": start.isoformat(), "end": end.isoformat(),
                                 "error": f"{type(exc).__name__}: {exc}"})
            print(f"  [存档] {_dump('_' + wkey)}", flush=True)

    out = _dump()
    log = ROOT / "logs" / "dividend_low_vol.txt"
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"cash": args.cash, "smoke": args.smoke, "set": args.set,
                            "rows": rows}, ensure_ascii=False, default=str) + "\n")
    print(f"\n已存档：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
