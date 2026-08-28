#!/usr/bin/env python
"""GAPCUT（低开竞价全砍）敏感性检验（P1，2026-08-15）。

背景：V7 一年回测 806 笔离场中 385 笔（48%）是 TAIL_V6_GAPCUT（开盘 ≤ 成本−0.3%
即竞价全砍），胜率 0%、合计 −72.4 万，超过全年总亏损。本脚本用日线回答一个关键问题：

**这些被砍的票，是"砍太早"（盘中/收盘其实收回成本）还是"选错票"（继续下跌）？**

方法
----
对平仓明细 CSV 里每笔 GAPCUT 交易，取 T+1（closed_at）日线：
* 收回率：当日最高 ≥ 成本（若不砍，盘中可保本离场）
* 收盘回本率：当日收盘 ≥ 成本（持有到收盘不亏）
* 继续下跌率：当日最低 < 实际砍仓价（砍在开盘确实优于等更低点）

替代阈值敏感性（全部离场交易，日线近似反事实）：
* L ∈ {−0.3%, −0.5%, −1.0%, −1.5%, −2.0%, 不砍}：
  开盘 gap < −L → 开盘市价砍；否则持有到当日收盘市价走。
  比较各阈值下的总盈亏 / 胜率 / 砍仓笔数 —— 判断 0.3% 是否砍太紧。

用法
----
    # 默认用最近一次一年回测平仓明细
    python scripts/tail_pick_gapcut_sensitivity.py

    # 指定明细 CSV / 指定砍仓阈值
    python scripts/tail_pick_gapcut_sensitivity.py --trades .verify_tmp/bt_v7_year_trades.csv

只读、只写 reports/，不修改策略、不交易。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from qmt_trade.datahub.types import Adjust, Freq  # noqa: E402

logger = logging.getLogger("gapcut_sens")

_DEFAULT_TRADES = _ROOT / ".verify_tmp" / "bt_v7_year_trades.csv"
#: 敏感性阈值（正数表示"开盘较成本低开超此比例才砍"）；None = 不砍（持有到收盘）
THRESHOLDS = [("−0.3%", 0.003), ("−0.5%", 0.005), ("−1.0%", 0.01),
              ("−1.5%", 0.015), ("−2.0%", 0.02), ("不砍(持有到收盘)", None)]


def _load_trades(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"平仓明细不存在：{path}")
    df = pd.read_csv(path)
    for col in ("opened_at", "closed_at"):
        if col not in df.columns:
            raise SystemExit(f"CSV 缺列 {col}（需要 symbol/opened_at/closed_at/reason/"
                             f"shares/entry_price/exit_price/pnl）")
    df["opened_at"] = pd.to_datetime(df["opened_at"]).dt.date
    df["closed_at"] = pd.to_datetime(df["closed_at"]).dt.date
    df["entry_price"] = df["entry_price"].astype(float)
    df["exit_price"] = df["exit_price"].astype(float)
    df["pnl"] = df["pnl"].astype(float)
    df["shares"] = df["shares"].astype(int)
    return df


def _bar_map(hub, symbols: list[str], start: date, end: date) -> dict[tuple[str, date], dict]:
    """(symbol, date) → 当日日线 dict（不复权）。缺失的返回 None（停牌/数据缺）。"""
    out: dict[tuple[str, date], dict] = {}
    if not symbols:
        return out
    df = hub.get_bars(list(symbols), Freq.D1, start, end, Adjust.NONE, validate=True)
    if df is None or df.empty:
        return out
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    for row in df.itertuples(index=False):
        out[(row.symbol, row.date.date())] = {
            "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close),
        }
    return out


def _sim_exit(bar: dict, entry: float, shares: int, cut_l: float | None,
              cost) -> tuple[float, str]:
    """日线近似离场：gap < −L 开盘砍，否则持有到收盘。返回 (净盈亏, 信号)。"""
    open_gap = bar["open"] / entry - 1.0
    if cut_l is not None and open_gap < -cut_l:
        px = bar["open"] * (1 - cost.fixed_slippage)   # 开盘市价 − 滑点
        sig = "CUT_OPEN"
    else:
        px = bar["close"] * (1 - cost.fixed_slippage)  # 收盘市价 − 滑点
        sig = "HOLD_CLOSE"
    amt = px * shares
    fee = cost.commission(amt) + cost.stamp_tax(amt) + cost.transfer(amt)
    return (px - entry) * shares - fee, sig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GAPCUT 敏感性：砍太早还是选错票（P1）")
    ap.add_argument("--trades", default=str(_DEFAULT_TRADES), help="平仓明细 CSV 路径")
    ap.add_argument("--config", default=None, help="配置文件，默认 config/settings.yaml")
    ap.add_argument("--mode", default="paper", help="数据源模式 sim/paper/live，默认 paper")
    ap.add_argument("--out", default=None, help="报告输出路径")
    args = ap.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from qmt_trade.app import build_context
    from qmt_trade.core.config import Settings
    from qmt_trade.execution.costs import CostModel

    settings = Settings.load(args.config) if args.config else None
    trades = _load_trades(Path(args.trades))
    logger.info("平仓明细 %d 笔（含 TP70/TRAIL 拆笔），文件 %s", len(trades), args.trades)

    with build_context(args.mode, settings=settings) as ctx:
        hub = ctx.hub
        cost = CostModel.from_settings(ctx.settings)

        syms = sorted(trades["symbol"].unique())
        d0 = trades["closed_at"].min() - timedelta(days=3)
        d1 = trades["closed_at"].max() + timedelta(days=1)
        bars = _bar_map(hub, syms, d0, d1)
        missing = [s for s in trades["symbol"].unique() if not any(k[0] == s for k in bars)]
        if missing:
            logger.warning("无日线数据的标的 %d 只（停牌/数据缺），其交易跳过", len(missing))

        rows = []
        for t in trades.itertuples(index=False):
            bar = bars.get((t.symbol, t.closed_at))
            if bar is None:
                continue
            entry, shares = float(t.entry_price), int(t.shares)
            rec = {"symbol": t.symbol, "reason": str(t.reason), "closed_at": t.closed_at,
                   "entry": entry, "pnl_actual": float(t.pnl),
                   "open_gap": bar["open"] / entry - 1.0,
                   "high_ge_entry": float(bar["high"]) >= entry,
                   "close_ge_entry": float(bar["close"]) >= entry,
                   "low_lt_exit": float(bar["low"]) < float(t.exit_price)}
            for label, L in THRESHOLDS:
                pnl_sim, sig = _sim_exit(bar, entry, shares, L, cost)
                rec[f"pnl_{label}"] = pnl_sim
                rec[f"sig_{label}"] = sig
            rows.append(rec)
        sim = pd.DataFrame(rows)
        if sim.empty:
            raise SystemExit("没有可撮合的交易（数据缺失？）")

        # ---- 1) GAPCUT 收回统计 ----
        gc = sim[sim["reason"] == "TAIL_V6_GAPCUT"]
        n_gc = len(gc)
        rec_rate = float(gc["high_ge_entry"].mean()) if n_gc else float("nan")
        close_rate = float(gc["close_ge_entry"].mean()) if n_gc else float("nan")
        worse_rate = float(gc["low_lt_exit"].mean()) if n_gc else float("nan")
        # 持有到收盘反事实
        hold_better = 0.0
        if n_gc:
            hold_better = float(
                ((gc["pnl_不砍(持有到收盘)"] - gc["pnl_actual"]) > 0).mean())
        act_sum = float(gc["pnl_actual"].sum())
        hold_sum = float(gc["pnl_不砍(持有到收盘)"].sum())

        # ---- 2) 阈值敏感性（全部离场交易）----
        sens = []
        for label, L in THRESHOLDS:
            col = f"pnl_{label}"
            pnls = sim[col].dropna()
            cuts = int((sim[f"sig_{label}"] == "CUT_OPEN").sum())
            sens.append({
                "阈值": label,
                "总盈亏": float(pnls.sum()),
                "胜率": float((pnls > 0).mean()) if len(pnls) else float("nan"),
                "砍仓笔数": cuts,
                "笔均": float(pnls.mean()) if len(pnls) else float("nan"),
            })

        # ---- 输出 ----
        lines = [
            f"# GAPCUT 敏感性：砍太早还是选错票（{Path(args.trades).name}）",
            "",
            f"- 平仓明细：{len(trades)} 笔 → 可撮合 {len(sim)} 笔"
            f"（{len(sim) - len(trades):+d} 笔因 T+1 无日线跳过）",
            f"- 其中 TAIL_V6_GAPCUT（低开 ≤ 成本−0.3% 竞价全砍）：{n_gc} 笔",
            f"- 成本口径：滑点 {cost.fixed_slippage:.1%}×2 + 佣金/印花/过户（与回测一致）",
            "",
            "## 一、GAPCUT 的 385 笔：不砍会怎样（日线反事实）",
            "",
            "| 指标 | 值 | 含义 |",
            "|---|---:|---|",
            f"| 盘中收回成本率 | {rec_rate:.1%} | 当日最高 ≥ 成本（不砍盘中可保本走） |" if n_gc else "| 盘中收回成本率 | n/a | 无样本 |",
            f"| 收盘回本率 | {close_rate:.1%} | 持有到收盘不亏 |" if n_gc else "| 收盘回本率 | n/a | 无样本 |",
            f"| 砍仓后继续下跌率 | {worse_rate:.1%} | 当日最低 < 实际砍仓价（砍在开盘优于等更低点） |" if n_gc else "| 砍仓后继续下跌率 | n/a | 无样本 |",
            f"| 持有到收盘更优占比 | {hold_better:.1%} | 不砍（持有到收盘）比实际砍仓盈亏更高 |" if n_gc else "| 持有到收盘更优占比 | n/a | 无样本 |",
            f"| 实际砍仓合计 | {act_sum:+,.0f} | 现有规则实现 |" if n_gc else "| 实际砍仓合计 | n/a | 无样本 |",
            f"| 持有到收盘反事实合计 | {hold_sum:+,.0f} | 全仓持有到 T+1 收盘 |" if n_gc else "| 持有到收盘反事实合计 | n/a | 无样本 |",
            "",
            "## 二、低开砍仓阈值敏感性（全部离场交易，日线近似反事实）",
            "",
            "| 砍仓阈值 | 总盈亏 | 胜率 | 砍仓笔数 | 笔均盈亏 |",
            "|---|---:|---:|---:|---:|",
        ]
        for s in sens:
            lines.append(f"| {s['阈值']} | {s['总盈亏']:+,.0f} | {s['胜率']:.1%} | "
                         f"{s['砍仓笔数']:,} | {s['笔均']:+,.0f} |")
        lines.append("")
        lines.append("> 注：日线近似反事实把『不砍』简化成『持有到收盘』，是上界估计——"
                     "真实日内链还有硬止损/保本/止盈，实际结果会介于『开盘砍』与『收盘走』之间。")
        report = "\n".join(lines)

        out_path = Path(args.out) if args.out else \
            _ROOT / "reports" / f"tail_pick_gapcut_sensitivity_{Path(args.trades).stem}.md"
        out_path.write_text(report + "\n", encoding="utf-8")

        print("\n" + "=" * 78)
        print(f"GAPCUT 敏感性（{Path(args.trades).name}）")
        print("=" * 78)
        print(report)
        print(f"\n已写入：{out_path}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
