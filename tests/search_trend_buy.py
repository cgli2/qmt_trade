#!/usr/bin/env python
"""趋势买点参数空间自动化搜索（2026-08-16）。

用法：
    # 阶段1：单参数敏感性（指定窗口）
    python scripts/search_trend_buy.py --window bull --set s1

    # 阶段2：候选组合 × 三窗口
    python scripts/search_trend_buy.py --window all --set finalists

进程内顺序运行（共享 DataHub，各变体独立回测器），结果表 + JSON 存档。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qmt_trade.app import build_context
from qmt_trade.core.config import Settings
from qmt_trade.strategies.base import load_config
from qmt_trade.strategies.trend_buy import TrendBuyBacktester, TrendBuyConfig

WINDOWS = {
    "bull": ("2025-06-24", "2026-06-25", "牛 2025-06~2026-06（沪深300 +28.6%）"),
    "mix": ("2024-01-02", "2025-06-30", "混合 2024-01~2025-06"),
    "choppy": ("2025-08-15", "2026-08-14", "震荡 2025-08~2026-08"),
}

# 基准（P3 定稿）
BASE = dict(pattern="breakout_pullback", market_ma_days=60, market_ma_days2=0,
            breakout_vol_mult=2.0, breakout_lookback=60, pullback_band=0.03,
            pullback_vol_shrink=0.5, pullback_window=(2, 3), pullback_hold_ma=10,
            take_profit1=0.20, take_profit2=0.35, tp1_sell_ratio=0.5,
            stop_floor_mult=0.95, stop_floor_pct=0.08, max_hold_days=20,
            max_positions=3, position_fraction=0.33)

# 阶段1：单参数敏感性（牛窗口，逐一偏离基准）
S1 = [
    ("基准 P3", {}),
    ("vol=1.5", {"breakout_vol_mult": 1.5}),
    ("vol=2.5", {"breakout_vol_mult": 2.5}),
    ("窗口(1,3)", {"pullback_window": (1, 3)}),
    ("窗口(2,5)", {"pullback_window": (2, 5)}),
    ("窗口(3,5)", {"pullback_window": (3, 5)}),
    ("回踩带2%", {"pullback_band": 0.02}),
    ("回踩带5%", {"pullback_band": 0.05}),
    ("缩量0.3", {"pullback_vol_shrink": 0.3}),
    ("缩量0.8", {"pullback_vol_shrink": 0.8}),
    ("站稳MA5", {"pullback_hold_ma": 5}),
    ("站稳MA20", {"pullback_hold_ma": 20}),
    ("TP2=0.30", {"take_profit2": 0.30}),
    ("TP2=0.50", {"take_profit2": 0.50}),
    ("TP1比例0.3", {"tp1_sell_ratio": 0.3}),
    ("TP1比例0.7", {"tp1_sell_ratio": 0.7}),
    ("止损0.92", {"stop_floor_mult": 0.92}),
    ("止损0.98", {"stop_floor_mult": 0.98}),
    ("止损兜底6%", {"stop_floor_pct": 0.06}),
    ("止损兜底10%", {"stop_floor_pct": 0.10}),
    ("持仓10日", {"max_hold_days": 10}),
    ("持仓30日", {"max_hold_days": 30}),
    ("4只×25%", {"max_positions": 4, "position_fraction": 0.25}),
    ("闸门MA40", {"market_ma_days": 40}),
    ("闸门MA80", {"market_ma_days": 80}),
    ("形态any(三合一)", {"pattern": "any"}),
    ("移动止盈 10/8", {"trail_enabled": True, "trail_activate_pct": 0.10, "trail_drawdown_pct": 0.08}),
    ("移动止盈 8/6", {"trail_enabled": True, "trail_activate_pct": 0.08, "trail_drawdown_pct": 0.06}),
]

# 阶段2b：C1（4只25%）基础上的微调（避免堆叠式过拟合，只试 2 个独立方向）
S2 = [
    ("C1 4只25%", {"max_positions": 4, "position_fraction": 0.25}),
    ("C1+TP1比例0.3", {"max_positions": 4, "position_fraction": 0.25,
                       "tp1_sell_ratio": 0.3}),
    ("C1+vol2.5", {"max_positions": 4, "position_fraction": 0.25,
                   "breakout_vol_mult": 2.5}),
]


def run_one(hub_ctx, cfg_ov: dict, start: date, end: date) -> dict:
    kw = dict(BASE)
    kw.update(cfg_ov)
    cfg = TrendBuyConfig(**kw)
    bt = TrendBuyBacktester(hub_ctx.settings, hub_ctx.hub, initial_cash=1_000_000,
                            config=cfg)
    res = bt.run(start, end)
    m, c = res.metrics or {}, res.cost_attribution or {}
    return {
        "ret": m.get("total_return"), "sharpe": m.get("sharpe"),
        "mdd": m.get("max_drawdown"), "win": m.get("win_rate"),
        "gross": c.get("gross_pnl", 0) / 1e6, "cost": c.get("cost_drag", 0) / 1e6,
        "net": c.get("net_pnl", 0) / 1e6, "turnover": c.get("single_side_turnover"),
        "n": len(res.closed_trades),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="bull", choices=list(WINDOWS) + ["all"])
    ap.add_argument("--set", default="s1", choices=["s1", "s2", "finalists"])
    ap.add_argument("--combos", default="", help="阶段2用，逗号分隔的 s1 名称（如 基准,vol2.5,TP2=0.30）")
    args = ap.parse_args()

    settings = Settings.load("config/settings.yaml")
    with build_context("paper", settings=settings) as ctx:
        if args.set == "s1":
            variants = S1
        elif args.set == "s2":
            variants = S2
        else:
            names = [n.strip() for n in args.combos.split(",") if n.strip()] if args.combos \
                else ["基准 P3"]
            pool = {n: ov for n, ov in S1 + S2}
            variants = [(n, pool.get(n, {})) for n in names] or S2

        if args.window == "all":
            windows = list(WINDOWS.items())
        else:
            windows = [(args.window, WINDOWS[args.window])]

        rows = []
        for wkey, (ws, we, wdesc) in windows:
            s, e = date.fromisoformat(ws), date.fromisoformat(we)
            print(f"\n===== 窗口 {wkey}: {wdesc} =====", flush=True)
            for name, ov in variants:
                try:
                    r = run_one(ctx, ov, s, e)
                    rows.append({"window": wkey, "name": name, **r})
                    print(f"  {name:<14} ret={r['ret']:+.2%} sharpe={r['sharpe']:+.2f} "
                          f"mdd={r['mdd']:+.2%} win={r['win']:.1%} gross={r['gross']:+.2%} "
                          f"net={r['net']:+.2%} 换手={r['turnover']:.1f}x n={r['n']}", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"  {name:<14} ERROR: {exc}", flush=True)
                    rows.append({"window": wkey, "name": name, "error": str(exc)})

        out = Path(".verify_tmp") / f"search_trend_buy_{args.set}_{args.window}.json"
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已存档：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
