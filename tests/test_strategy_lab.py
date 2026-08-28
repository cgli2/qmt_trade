"""策略实验室四策略机制冒烟（合成数据，验证逻辑而非业绩）。

对每类策略构造一个必然触发的场景，断言：有成交/有平仓、成本恒等式
（净 = 毛 − 滑点 − 费用）成立、结果对象字段齐全。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from qmt_trade.datahub.types import InstrumentInfo  # noqa: E402
from qmt_trade.strategies.limit_up import LimitUpBacktester, LimitUpConfig  # noqa: E402
from qmt_trade.strategies.second_board import SecondBoardBacktester, SecondBoardConfig  # noqa: E402
from qmt_trade.strategies.dip_buy import DipBuyBacktester, DipBuyConfig  # noqa: E402
from qmt_trade.strategies.trend_buy import TrendBuyBacktester, TrendBuyConfig  # noqa: E402

# 交易日历：2026-06-01 起 95 个工作日（趋势族需 ≥70 行算 MA60）
_DAYS = [d for d in (date(2026, 6, 1) + timedelta(days=i) for i in range(160)) if d.weekday() < 5][:95]


class _Settings:
    def section(self, key):
        if key == "execution.costs":
            return {"commission_rate": 0.0001, "commission_min": 5.0,
                    "stamp_duty_rate": 0.0005, "transfer_fee_rate": 1.0e-05,
                    "base_slippage": 0.002}
        return {}

    def get(self, key, default=None):
        return default


class FakeHub:
    """日线 FakeHub（含 limit_up/prev_close 列，供打板族/趋势族使用）。"""

    def __init__(self, daily: pd.DataFrame, instruments: dict):
        self.daily = daily
        self.instruments = instruments
        self.index_daily = pd.DataFrame({
            "date": [pd.Timestamp(d) for d in _DAYS],
            "symbol": ["000300.SH"] * len(_DAYS),
            "open": 4000.0, "high": 4000.0, "low": 4000.0,
            "close": [4000 + i for i in range(len(_DAYS))], "volume": 1,
        })

    def get_bars(self, symbols, freq="1d", start=None, end=None, adjust=None, validate=True):
        df = self.daily.copy()
        if symbols is not None:
            syms = symbols if isinstance(symbols, list) else [symbols]
            df = df[df["symbol"].isin(syms)]
        if start is not None:
            df = df[pd.to_datetime(df["date"]) >= pd.Timestamp(start)]
        if end is not None:
            df = df[pd.to_datetime(df["date"]) <= pd.Timestamp(end)]
        return df.reset_index(drop=True)

    def get_instruments(self, symbols=None):
        if symbols is None:
            return list(self.instruments.values())
        return [i for i in self.instruments.values() if i.symbol in set(symbols)]

    def get_index_bars(self, symbol, start=None, end=None):
        df = self.index_daily.copy()
        if start is not None:
            df = df[pd.to_datetime(df["date"]) >= pd.Timestamp(start)]
        if end is not None:
            df = df[pd.to_datetime(df["date"]) <= pd.Timestamp(end)]
        return df.reset_index(drop=True)


def _inst(sym):
    return InstrumentInfo(symbol=sym, name=sym, industry="测试", list_date=date(2015, 1, 1),
                          total_share=1_000_000_000, float_share=500_000_000,
                          is_st=False, is_suspended=False, market_cap=1e10)


def _row(sym, d, o, h, l, c, v=1_000_000, lu=None):
    pc = _row._prev.get(sym, round(o / 1.01, 4))
    _row._prev[sym] = c
    return dict(date=pd.Timestamp(d), symbol=sym, open=o, high=h, low=l, close=c,
                volume=v, amount=v * c, prev_close=pc,
                limit_up=lu if lu is not None else round(c * 1.1, 4),
                limit_down=round(c * 0.9, 4), is_suspended=False)


_row._prev = {}


def _build_hub(scenario: str):
    """构造合成市场。历史长度按策略需求（趋势族需 ≥70 行算 MA60/60日新高）。"""
    daily: list[dict] = []
    n = 90
    for i in range(n):
        d = _DAYS[i]
        base = 10.0
        if scenario == "limit_up":
            sym = "600001.SH"
            # 前 80 天平走 → 二连板 → 高开买入日 → 破板
            if i < 80:
                daily.append(_row(sym, d, base, base + 0.1, base - 0.1, base))
            elif i == 80:
                daily.append(_row(sym, d, 10.9, 11.1, 10.8, 11.0, lu=11.0))     # 首板
            elif i == 81:
                daily.append(_row(sym, d, 11.6, 12.2, 11.5, 12.1, lu=12.1))     # 二板（换手板，open<涨停价）
            elif i == 82:
                daily.append(_row(sym, d, 12.6, 12.7, 12.4, 12.6))              # 高开+4.1% 买入日
            elif i == 83:
                daily.append(_row(sym, d, 13.9, 14.0, 13.6, 13.86, lu=13.86))   # 续板持有
            elif i == 84:
                daily.append(_row(sym, d, 13.8, 14.2, 13.5, 13.9))              # 破板 → 次日卖
            else:
                daily.append(_row(sym, d, 13.9, 14.1, 13.6, 13.8))
        elif scenario == "dip_buy":
            sym = "600002.SH"
            if i < 80:
                daily.append(_row(sym, d, base, base + 0.1, base - 0.1, base))
            elif i == 80:
                daily.append(_row(sym, d, base, base * 1.01, base * 0.99, base * 1.06))   # 大阳突破
            elif i == 81:
                daily.append(_row(sym, d, base * 1.05, base * 1.07, base * 1.04, base * 1.06))  # 尾盘买
            elif i == 82:
                daily.append(_row(sym, d, base * 1.06, base * 1.16, base * 1.05, base * 1.15))  # +9% TP1
            else:
                daily.append(_row(sym, d, base * 1.15, base * 1.17, base * 1.13, base * 1.16))
        elif scenario == "trend":
            sym = "600003.SH"
            if i < 60:
                daily.append(_row(sym, d, base, base + 0.1, base - 0.1, base))
            elif i < 80:
                # 缓慢上行（MA20/MA60 走平微升）
                px = base * (1 + (i - 59) * 0.002)
                daily.append(_row(sym, d, px, px * 1.01, px * 0.99, px))
            elif i == 80:
                daily.append(_row(sym, d, base * 1.05, base * 1.07, base * 1.04, base * 1.06, v=5_000_000))  # 放量突破
            elif i == 81:
                daily.append(_row(sym, d, base * 1.05, base * 1.055, base * 1.02, base * 1.04, v=800_000))   # 缩量回踩
            elif i == 82:
                daily.append(_row(sym, d, base * 1.04, base * 1.05, base * 1.03, base * 1.045))             # 买入日
            elif i == 85:
                daily.append(_row(sym, d, base * 1.05, base * 1.27, base * 1.04, base * 1.26))              # +21% TP1
            else:
                daily.append(_row(sym, d, base * 1.045, base * 1.06, base * 1.03, base * 1.05))
    hub = FakeHub(pd.DataFrame(daily), {_SYM[scenario]: _inst(_SYM[scenario])})
    return hub


_SYM = {"limit_up": "600001.SH", "dip_buy": "600002.SH", "trend": "600003.SH"}


def _check(name: str, bt, start_idx: int = 2, end_idx: int = 25) -> bool:
    res = bt.run(_DAYS[start_idx], _DAYS[end_idx])
    ca = res.cost_attribution or {}
    ok = bool(res.trades) and bool(res.closed_trades) and bool(ca)
    if ok:
        drag = float(ca.get("cost_drag", 0) or 0)
        ok &= abs(float(ca.get("gross_pnl", 0) or 0)
                  - (float(ca.get("net_pnl", 0) or 0) + drag)) < 0.01
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: trades={len(res.trades)} "
          f"closed={len(res.closed_trades)} "
          f"ret={res.metrics.get('total_return') if res.metrics else None}")
    return ok


def main() -> int:
    checks = []
    # 方向一 打板（买入日 i=82）
    hub = _build_hub("limit_up")
    bt = LimitUpBacktester(_Settings(), hub, initial_cash=1_000_000,
                           config=LimitUpConfig(industry_map_path="", exclude_holiday=False,
                                                exclude_weekend=False, hot_sector_top_n=0))
    checks.append(_check("打板 limit_up", bt, start_idx=82, end_idx=92))
    # 方向二 二板（同一场景）
    bt2 = SecondBoardBacktester(_Settings(), hub, initial_cash=1_000_000,
                                config=SecondBoardConfig(industry_map_path="", exclude_holiday=False,
                                                         exclude_weekend=False, hot_sector_top_n=0))
    checks.append(_check("二板 second_board", bt2, start_idx=82, end_idx=92))
    # 方向三 尾盘低吸（买入日 i=81）
    hub3 = _build_hub("dip_buy")
    bt3 = DipBuyBacktester(_Settings(), hub3, initial_cash=1_000_000,
                           config=DipBuyConfig(industry_map_path="", pattern="breakout"))
    checks.append(_check("尾盘低吸 dip_buy", bt3, start_idx=81, end_idx=90))
    # 方向四 趋势买点（买入日 i=82）
    hub4 = _build_hub("trend")
    bt4 = TrendBuyBacktester(_Settings(), hub4, initial_cash=1_000_000,
                             config=TrendBuyConfig(pattern="breakout_pullback"))
    checks.append(_check("趋势买点 trend_buy", bt4, start_idx=82, end_idx=92))

    ok = all(checks)
    print("\n策略实验室冒烟:", "ALL PASS ✅" if ok else "FAILED ❌")

    # ---- 集成点：池种子权重 + 调度注册 ----
    from qmt_trade.core.config import Settings
    from qmt_trade.evolution.pool import StrategyPool
    from qmt_trade.scheduler.jobs import JOB_MAP
    from qmt_trade.strategies.live import ENTRY_PHASE
    p = StrategyPool(Settings.load("config/settings.yaml"))
    seed_ok = p.strategies.get("trend_buy") is not None \
        and p.strategies["trend_buy"].weight == 0.15 \
        and p.strategies["trend_buy"].status == "ACTIVE"
    job_ok = "strategylab_open" in JOB_MAP and "strategylab_run" in JOB_MAP
    phase_ok = ENTRY_PHASE == {"limit_up": "open", "second_board": "open",
                               "dip_buy": "close", "trend_buy": "close"}
    for name, passed in (("池种子权重 trend_buy=0.15 ACTIVE", seed_ok),
                         ("调度注册 strategylab_open/run", job_ok),
                         ("入场相位映射", phase_ok)):
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")

    # ---- 执行一致性修复（2026-08-16）：external_stops + STRUCTURE 绝对价止损 ----
    from qmt_trade.risk.engine import RiskEngine
    from qmt_trade.risk.killswitch import KillSwitch
    from qmt_trade.portfolio.state import PortfolioState
    from qmt_trade.core.trading import Fill, Position
    from qmt_trade.execution.service import ExecutionService
    from qmt_trade.brain.schemas import TradeIntent

    # 1) _stop_price：STRUCTURE + 绝对价(>1) → 返回绝对价
    svc_stub = object.__new__(ExecutionService)
    it = TradeIntent(symbol="X", action="BUY", confidence=0.9, conviction="MEDIUM",
                     stop_loss_type="STRUCTURE", stop_loss_value=9.50,
                     valid_until=_DAYS[5])
    sp = ExecutionService._stop_price(svc_stub, it, 10.0)
    # 2) guard_positions external_stops：外部持仓只在外部止损位触发，且跳过主系统规则
    ps = PortfolioState(cash=1_000_000)
    ps.positions["600000.SH"] = Position(symbol="600000.SH", shares=1000,
                                         avg_cost=10.0, can_use=1000,
                                         stop_loss_price=7.0,  # 主系统默认止损（-7%）
                                         highest_since_open=12.0)  # 若走 trailing 会触发
    ps.positions["600000.SH"].opened_at = _DAYS[5]
    engine = RiskEngine(Settings.load("config/settings.yaml"))
    acts = engine.guard_positions(
        ps, last_prices={"600000.SH": 9.0}, asof=_DAYS[6],
        killswitch=KillSwitch(), external_stops={"600000.SH": 9.5})
    ok_ext = any(a.symbol == "600000.SH" and a.tag == "STOP_LOSS" for a in acts) \
        and not any(a.tag in ("TRAILING", "TIME_STOP", "TP_PARTIAL") for a in acts)
    # 3) 外部止损价之上不触发
    acts2 = engine.guard_positions(
        ps, last_prices={"600000.SH": 9.6}, asof=_DAYS[6],
        killswitch=KillSwitch(), external_stops={"600000.SH": 9.5})
    ok_none = not any(a.symbol == "600000.SH" for a in acts2)
    for name, passed in (("STRUCTURE 绝对价止损", abs(sp - 9.50) < 1e-9),
                         ("external_stops 生效且跳过主系统规则", ok_ext),
                         ("外部止损价之上不触发", ok_none)):
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("\n策略实验室冒烟+集成:", "ALL PASS ✅" if ok else "FAILED ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
