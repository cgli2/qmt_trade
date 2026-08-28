import sys, logging, datetime, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.WARNING)

from qmt_trade.app import build_context
from qmt_trade.strategies.etf_t0 import ETFT0Backtester, ETFT0Config

start, end = datetime.date(2026, 5, 18), datetime.date(2026, 8, 18)
with build_context("paper") as ctx:
    for label, override in (("默认(先卖后买)", {}),
                            ("真T+0双向(same_day_roundtrip)", {"same_day_roundtrip": True})):
        cfg = ETFT0Config(**override)
        bt = ETFT0Backtester(ctx.settings, ctx.hub, initial_cash=1_000_000.0, config=cfg)
        # 包装 _intraday_t0 捕获逐日 T0 盈亏（result 未暴露 day_pnl）
        day_pnl_list = []
        _orig = bt._intraday_t0
        def _wrap(day, universe, t0):
            pnl = _orig(day, universe, t0)
            day_pnl_list.append(pnl)
            return pnl
        bt._intraday_t0 = _wrap
        res = bt.run(start, end)
        m = res.metrics or {}
        real_t0_pnl = round(sum(day_pnl_list), 2)
        print("=" * 66)
        print(f"[{label}]")
        print(f"  区间收益 {m.get('total_return')}   最大回撤 {m.get('max_drawdown')}")
        print(f"  T0腿数 {m.get('t0_legs')}  胜腿 {m.get('t0_win_legs')}  亏腿 {m.get('t0_loss_legs')}")
        print(f"  T0真实日内盈亏(逐日求和) {real_t0_pnl:,.2f}   有T交易日 {sum(1 for p in day_pnl_list if abs(p) > 1e-9)}")
        print(f"  metrics['t0_pnl'](疑似bug) = {m.get('t0_pnl')}")
        print(f"  净盈亏(全含成本) {m.get('net_profit') if m.get('net_profit') is not None else 'n/a'}")
        if m:
            for k in ("start_equity", "end_equity", "win_rate", "n_trades"):
                print(f"    {k} = {m.get(k)}")
