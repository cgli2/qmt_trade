"""个股存量持仓做T（stock_t0）冒烟测试。

覆盖：
1. 注册：JOB_MAP / STANDALONE_STRATEGIES / JOB_DESCRIPTIONS / settings.yaml 配置段加载。
2. Mock 回测（无分钟线）：工程正确性 —— 存量底仓建立、回测不崩、T0 腿数为 0。
3. 合成分钟线回测：真实跑通「高抛 → 回落买回」单腿闭环，T0 盈利、胜腿数>0、
   底仓股数不变（尾盘 T 仓归零 = 降成本）。
4. ExecutionService.exact_buy_shares：精确回补路径，不覆盖持仓风控元数据。
5. LiveRunner 早退路径 + 工具函数（_slice_qty / _interval_ok / mom_sell_ok）。

运行：python tests/smoke_stock_t0.py （需 pandas/numpy，系统 Python 3.11+）
"""
from __future__ import annotations

import io
import logging
import sys
from datetime import date, datetime, time as dtime

import pandas as pd

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        logger.info("  [OK]   %s %s", name, extra)
    else:
        FAIL += 1
        logger.info("  [FAIL] %s %s", name, extra)


# ---------------------------------------------------------------- [1] 注册与配置
def test_registration() -> None:
    logger.info("\n[1] 注册与配置段")
    from qmt_trade.scheduler.jobs import JOB_MAP
    from qmt_trade.scheduler.runner import JOB_DESCRIPTIONS
    from qmt_trade.core.strategies import (STANDALONE_STRATEGIES,
                                           is_standalone_strategy,
                                           build_standalone_backtester)
    from qmt_trade.core.config import Settings

    check("JOB_MAP 含 stock_t0_intraday", "stock_t0_intraday" in JOB_MAP)
    check("JOB_DESCRIPTIONS 含 stock_t0_intraday",
          "stock_t0_intraday" in JOB_DESCRIPTIONS)
    check("STANDALONE_STRATEGIES 含 stock_t0", "stock_t0" in STANDALONE_STRATEGIES)
    check("is_standalone_strategy(stock_t0)", is_standalone_strategy("stock_t0"))
    st = Settings.load()
    cfg = st.section("strategies.stock_t0") or {}
    check("settings.yaml 含 strategies.stock_t0", bool(cfg),
          f"enabled={cfg.get('enabled')}")
    # 保守默认是 false；用户主动开启后（模拟盘自动运行）为 true —— 断言类型即可
    check("stock_t0 enabled 为布尔配置", isinstance(cfg.get("enabled"), bool))
    check("scheduler.jobs 含 stock_t0_start",
          "stock_t0_start" in (st.section("scheduler.jobs") or {}))
    try:
        bt = build_standalone_backtester("stock_t0", st, None, initial_cash=1_000_000)
        check("build_standalone_backtester(stock_t0) 构造成功", bt is not None,
              type(bt).__name__)
    except Exception as exc:                           # noqa: BLE001
        check("build_standalone_backtester(stock_t0) 构造成功", False, str(exc))


# ---------------------------------------------------------------- [2] Mock 回测
def test_mock_backtest() -> None:
    logger.info("\n[2] Mock 回测（无分钟线 → 仅持底仓，工程正确性）")
    from qmt_trade.core.config import Settings
    from qmt_trade.datahub.manager import DataHub
    from qmt_trade.datahub.providers.mock import MockProvider
    from qmt_trade.strategies.stock_t0 import StockT0Config, StockT0Backtester

    st = Settings.load()
    mock = MockProvider(n_symbols=8, start="2025-01-02", end="2026-08-07", seed=7)
    hub = DataHub(st, [mock])
    sym = mock.symbols[0]
    cfg = StockT0Config(symbols=[sym], base_fraction=0.2)
    bt = StockT0Backtester(st, hub, initial_cash=1_000_000, config=cfg)
    res = bt.run(date(2025, 1, 2), date(2025, 3, 3))
    m = res.metrics or {}
    check("回测不崩且产出 metrics", bool(m), f"n_days={m.get('n_days')}")
    check("存量底仓已建立", sym in {p["symbol"] for p in res.open_positions},
          f"shares={[p['shares'] for p in res.open_positions]}")
    check("无分钟线时不做T（t0_legs=0）", m.get("t0_legs") == 0,
          f"minute_available={m.get('minute_available')}")
    check("metrics 携带 t0 统计字段",
          {"t0_pnl", "t0_legs", "t0_win_legs", "t0_loss_legs"} <= set(m))


# ---------------------------------------------------------------- [3] 合成分钟线
def _make_minute_frame(day: date) -> pd.DataFrame:
    """构造一个高抛→回落可回补的分钟序列（off 动量模式，确定性触发）。

    前 40 分钟平盘 10.0（VWAP=10.0）→ 第 41 分钟瞬间拉高到 10.20（+2.0%，
    触发卖高，振幅 2.3% 远离护栏边界）→ 之后回落 10.0（较开腿价 -2.0%
    < -0.5%，触发网格买回）。卖出后不再冲高，避免触发止损（保证单腿盈利）。
    """
    times = pd.date_range(f"{day} 09:31", f"{day} 15:00", freq="1min")
    n = len(times)
    closes, highs, lows = [], [], []
    for i in range(n):
        if i < 40:                       # 平盘蓄势
            c, h, l = 10.0, 10.02, 9.98
        elif i == 40:                    # 单分钟拉高到 +2%（触发卖高）
            c, h, l = 10.20, 10.21, 10.19
        else:                            # 回落 10.0（触发网格买回）
            c, h, l = 10.0, 10.01, 9.99
        closes.append(round(c, 2))
        highs.append(round(h, 2))
        lows.append(round(l, 2))
    return pd.DataFrame({
        "date": times, "symbol": "TEST.SH",
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": [10000.0] * n, "amount": [c * 10000.0 for c in closes],
        "time": times.time,
    })


def test_synthetic_minute_backtest() -> None:
    logger.info("\n[3] 合成分钟线：高抛→回落买回 单腿闭环")
    from qmt_trade.core.config import Settings
    from qmt_trade.datahub.manager import DataHub
    from qmt_trade.datahub.providers.mock import MockProvider
    from qmt_trade.strategies.stock_t0 import StockT0Config, StockT0Backtester

    st = Settings.load()
    cfg = StockT0Config(
        symbols=["TEST.SH"],
        initial_shares={"TEST.SH": 1000},
        base_fraction=0.2,
        momentum_mode="off",             # 关闭动量过滤，确定性触发
        min_leg_notional=0,              # 关闭名义金额护栏（测试小仓）
        min_minutes_per_day=5,
        max_trades_per_symbol_per_day=2,
    )
    bt = StockT0Backtester(st, hub=None, initial_cash=1_000_000, config=cfg)

    # 用 mock 仅取交易日历；行情全部注入
    mock = MockProvider(n_symbols=2, start="2025-01-02", end="2026-08-07", seed=3)
    hub = DataHub(st, [mock])
    bt.hub = hub
    days = bt._trading_days(date(2025, 1, 2), date(2025, 1, 15))
    check("合成测试有 ≥3 个交易日", len(days) >= 3, f"days={days[:3]}")

    # 注入前两天分钟线 + 日线（prev_close=10.0），其余日子走 mock 兜底
    for d in days[:2]:
        bt._minute_cache[("TEST.SH", d)] = _make_minute_frame(d)
        bt._day_cache[("TEST.SH", d)] = {
            "symbol": "TEST.SH", "date": d, "open": 10.0, "high": 10.3,
            "low": 9.9, "close": 10.0, "volume": 1e6, "amount": 1e7,
            "prev_close": 10.0, "limit_up": 11.0, "limit_down": 9.0,
            "is_suspended": False,
        }

    res = bt.run(days[0], days[-1])
    m = res.metrics or {}
    check("跑出 T0 腿数 ≥2（两天各一腿）", m.get("t0_legs", 0) >= 2,
          f"legs={m.get('t0_legs')} win={m.get('t0_win_legs')} loss={m.get('t0_loss_legs')}")
    check("T0 净盈利 > 0", (m.get("t0_pnl") or 0) > 0, f"t0_pnl={m.get('t0_pnl')}")
    check("胜腿数 ≥ 腿数（无亏损腿）", m.get("t0_loss_legs", 0) == 0,
          f"win={m.get('t0_win_legs')}")
    pos = {p["symbol"]: p for p in res.open_positions}
    check("底仓股数不变（1000 股）", pos.get("TEST.SH", {}).get("shares") == 1000,
          f"shares={pos.get('TEST.SH', {}).get('shares')}")
    check("期末 open_positions 仅含 TEST.SH", len(res.open_positions) == 1)


# ---------------------------------------------------------------- [4] 执行层精确回补
def test_exact_buy_shares() -> None:
    logger.info("\n[4] ExecutionService.exact_buy_shares 精确回补")
    from qmt_trade.app import build_context
    from qmt_trade.core.trading import Position
    from qmt_trade.brain.schemas import TradeIntent
    from qmt_trade.features.regime import Regime, RegimeSnapshot
    from qmt_trade.datahub.types import Bar
    from qmt_trade.datahub.providers.mock import MockProvider

    mock = MockProvider(n_symbols=6, start="2025-01-02", end="2026-08-07", seed=5)
    with build_context("paper", providers=[mock]) as ctx:
        sym = mock.symbols[0]
        pos = Position(symbol=sym, shares=1000, avg_cost=10.0, can_use=1000,
                       opened_at=date(2020, 1, 2),
                       stop_loss_price=9.5, stop_loss_type="FIXED_PCT",
                       max_holding_days=30)
        ctx.portfolio.positions[sym] = pos
        bar = Bar(symbol=sym, date=date.today(), open=10.0, high=10.2, low=9.9,
                  close=10.0, volume=1e6, amount=1e7,
                  limit_up=11.0, limit_down=9.0)
        reg = RegimeSnapshot(asof=date.today(), regime=Regime.RANGE, max_position=0.5,
                             min_score=0.0, min_percentile=0.7)
        instr = ctx.hub.get_instrument(sym)
        it = TradeIntent(symbol=sym, action="BUY", confidence=0.9,
                         conviction="MEDIUM", entry_type="LIMIT",
                         entry_ref_price=10.0, stop_loss_type="FIXED_PCT",
                         stop_loss_value=0.005, risk_budget_hint=0.3,
                         max_weight_hint=0.03, time_horizon_days=5,
                         max_holding_days=20, valid_until=date.today(),
                         reasoning="test buyback")
        res = ctx.execution.submit_intent(
            it, bar=bar, market_day=date.today(), asof=date.today(),
            regime=reg, instrument=instr, sym_industry={sym: ""},
            plan_id="stk0_test", seq=1, signal="STOCK_T0_BUYBACK",
            exact_buy_shares=300)
        check("精确回补成交且股数=300", res.ok and res.shares == 300,
              f"shares={res.shares} rejected={res.rejected_by} reason={res.reason}")
        p2 = ctx.portfolio.positions[sym]
        check("回补后持仓 1300 股（底仓+回补）", p2.shares == 1300,
              f"shares={p2.shares}")
        check("回补不覆盖原止损（9.5 保留）", abs((p2.stop_loss_price or 0) - 9.5) < 1e-6,
              f"stop={p2.stop_loss_price}")
        check("回补不覆盖原持有期（30 保留）", p2.max_holding_days == 30,
              f"max_holding_days={p2.max_holding_days}")

        # 非精确路径（exact_buy_shares=0）仍走 sizer + 覆盖风控元数据（原行为不变）
        sym2 = mock.symbols[1]
        pos2 = Position(symbol=sym2, shares=500, avg_cost=12.0, can_use=500,
                        opened_at=date(2020, 1, 2))
        ctx.portfolio.positions[sym2] = pos2
        bar2 = Bar(symbol=sym2, date=date.today(), open=12.0, high=12.3,
                   low=11.9, close=12.0, volume=1e6, amount=1.2e7,
                   limit_up=13.2, limit_down=10.8)
        it2 = TradeIntent(symbol=sym2, action="ADD", confidence=0.9,
                          conviction="MEDIUM", entry_type="LIMIT",
                          entry_ref_price=12.0, stop_loss_type="FIXED_PCT",
                          stop_loss_value=0.05, risk_budget_hint=0.3,
                          max_weight_hint=0.03, time_horizon_days=5,
                          max_holding_days=20, valid_until=date.today(),
                          reasoning="test add")
        res2 = ctx.execution.submit_intent(
            it2, bar=bar2, market_day=date.today(), asof=date.today(),
            regime=reg, instrument=ctx.hub.get_instrument(sym2),
            sym_industry={sym2: ""}, plan_id="main_test", seq=2,
            signal="REBALANCE")
        check("非精确路径正常成交（原行为）", res2.ok,
              f"rejected={res2.rejected_by} reason={res2.reason}")
        p3 = ctx.portfolio.positions[sym2]
        check("非精确路径仍覆盖风控元数据（原行为）",
              abs((p3.stop_loss_price or 0) - 12.0 * 0.95) < 1e-6,
              f"stop={p3.stop_loss_price}")


# ---------------------------------------------------------------- [5] LiveRunner 早退 + 工具
def test_live_early_returns() -> None:
    logger.info("\n[5] LiveRunner 早退路径 + 工具函数")
    from qmt_trade.strategies.stock_t0 import (StockT0LiveRunner, StockT0Config,
                                               mom_sell_ok)

    class _FakeJR:
        today = date(2026, 8, 18)

        class _Ctx:
            settings = None

        ctx = _Ctx()

    cfg_off = StockT0Config(enabled=False)
    cfg_empty = StockT0Config(enabled=True, symbols=[])

    class _FakeLive(StockT0LiveRunner):
        def _cfg(self):
            return self._test_cfg

    r = _FakeLive(_FakeJR)
    r._test_cfg = cfg_off
    out = r.tick()
    check("enabled=false → skipped", out.get("skipped") is True,
          out.get("reason", ""))
    r._test_cfg = cfg_empty
    out2 = r.tick()
    check("symbols 为空 → skipped", out2.get("skipped") is True,
          out2.get("reason", ""))

    cfg = StockT0Config(t_slice_ratio=0.3)
    check("_slice_qty(1000)=300", StockT0LiveRunner._slice_qty(1000, cfg) == 300)
    check("_slice_qty(300)=100（至少留一手）",
          StockT0LiveRunner._slice_qty(300, cfg) == 100)
    check("_slice_qty(150)=0（底仓不足）",
          StockT0LiveRunner._slice_qty(150, cfg) == 0)
    check("_interval_ok 间隔足够", StockT0LiveRunner._interval_ok(
        "10:00", dtime(10, 6), 5))
    check("_interval_ok 间隔不足", not StockT0LiveRunner._interval_ok(
        "10:05", dtime(10, 6), 5))
    c_f = StockT0Config(momentum_mode="filter", momentum_threshold=0.004)
    check("filter：强涨不卖高", not mom_sell_ok(c_f, 0.01))
    check("filter：未强涨可卖高", mom_sell_ok(c_f, 0.002))
    c_c = StockT0Config(momentum_mode="confirm", momentum_threshold=0.004)
    check("confirm：强涨才卖高", mom_sell_ok(c_c, 0.01))
    check("confirm：未强涨不卖高", not mom_sell_ok(c_c, 0.002))


def main() -> int:
    test_registration()
    test_mock_backtest()
    test_synthetic_minute_backtest()
    test_exact_buy_shares()
    test_live_early_returns()
    logger.info("\n%s\n结果: %d 通过 / %d 失败\n%s", "=" * 52, PASS, FAIL, "=" * 52)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

