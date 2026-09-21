"""诊断：趋势突破信号为何未成交（逐条打印过滤环节）。"""
from __future__ import annotations

import datetime
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.WARNING)

import pandas as pd

from qmt_trade.app import build_context
from qmt_trade.core.config import Settings
from qmt_trade.datahub.types import Adjust, Freq
from qmt_trade.strategies.trend_breakout import TrendBreakoutBacktester, TrendBreakoutConfig

START, END = datetime.date(2026, 6, 1), datetime.date(2026, 9, 18)

settings = Settings.load(str(ROOT / "config" / "settings.yaml"))
with build_context("paper", settings=settings) as ctx:
    hub = ctx.hub
    infos = hub.get_instruments()
    syms = [s for s in (list(infos.keys()) if isinstance(infos, dict)
                        else [getattr(i, "symbol", "") for i in (infos or [])]) if s][:300]
    bt = TrendBreakoutBacktester(settings, hub, initial_cash=1_000_000,
                                 config=TrendBreakoutConfig())
    bt.universe = syms
    bt._prewarm(START, END)
    p = bt._panel
    print("panel rows:", 0 if p is None else len(p), "cols:", list(p.columns)[:20])
    if p is None or p.empty:
        sys.exit(1)
    for c in ("t1", "t2", "t3", "t4", "t5", "t6", "signal"):
        print(f"  {c}: {int(p[c].sum())}")
    sig = p[p["signal"]]
    print("\n信号明细：")
    instr_map = bt._instrument_map(syms)
    for r in sig.itertuples(index=False):
        d = pd.to_datetime(r.date).date()
        sym = str(r.symbol)
        instr = instr_map.get(sym)
        prev = bt._prev_trading_day(d)
        bar = bt._bar(sym, d)
        hard = bt._hard_ok(sym, prev or d, instr)
        board = getattr(instr, "board", None) if instr else None
        st = getattr(instr, "is_st", None) if instr else None
        ld = getattr(instr, "list_date", None) if instr else None
        print(f"{d} {sym} close={r.close:.2f} zf={r.zf:.1f} vr={r.vol_ratio:.2f} "
              f"hj={r.hj:.2f} ld={r.ld:.2f} | bar={'Y' if bar else 'N'} "
              f"hard_ok={hard} board={board} st={st} list_date={ld} "
              f"prev={prev} bar_prev={'Y' if bt._bar(sym, prev or d) else 'N'} "
              f"limit_up={None if not bar else bar.get('limit_up')}")
        if not hard and bar:
            # 逐项排查
            print("     - suspended:", bar.get("is_suspended"),
                  " st:", st, " list_days:", None if not ld else (d - ld).days,
                  " board:", board)
