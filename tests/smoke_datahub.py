"""L0 数据层冒烟测试：DataHub + MockProvider 全链路。

覆盖：多源调度、熔断降级、缓存、PIT 切片、质量校验、极端场景注入。
运行：python tests/smoke_datahub.py
"""
from __future__ import annotations
import logging

import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qmt_trade.core.config import get_settings  # noqa: E402
from qmt_trade.core.errors import DataUnavailableError, LookAheadError  # noqa: E402
from qmt_trade.datahub.manager import DataHub  # noqa: E402
from qmt_trade.datahub.pit import PITGuard, assert_no_lookahead  # noqa: E402
from qmt_trade.datahub.providers.base import Capability, DataProvider  # noqa: E402
from qmt_trade.datahub.providers.mock import MockProvider  # noqa: E402
from qmt_trade.datahub.types import Freq  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger(__name__)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        logger.info(f"  [OK]   {name} {extra}")
    else:
        FAIL += 1
        logger.info(f"  [FAIL] {name} {extra}")


class BrokenProvider(DataProvider):
    """永远抛错的数据源，用于验证熔断与降级。"""

    name = "broken"
    capabilities = {Capability.BARS, Capability.INSTRUMENTS}

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def get_bars(self, *a, **kw):
        self.calls += 1
        raise RuntimeError("模拟数据源宕机")

    def get_instruments(self, *a, **kw):
        self.calls += 1
        raise RuntimeError("模拟数据源宕机")


def main() -> int:
    st = get_settings()
    st.set("datahub.priority.bars", ["broken", "mock"])
    st.set("datahub.priority.instruments", ["mock"])
    st.set("datahub.circuit_breaker.fail_threshold", 2)

    mock = MockProvider(n_symbols=30, start="2025-01-02", end="2026-08-07")
    broken = BrokenProvider()
    hub = DataHub(st, [broken, mock])
    syms = mock.symbols[:5]

    logger.info("\n[1] 基础行情获取")
    df = hub.get_bars(syms[0], Freq.D1, "2026-01-05", "2026-06-30")
    check("返回非空", not df.empty, f"rows={len(df)}")
    check("列齐全", {"symbol", "date", "open", "high", "low", "close", "volume"} <= set(df.columns))
    check("按时间升序", df["date"].is_monotonic_increasing)
    check("OHLC 自洽", bool(((df["high"] >= df["low"]) & (df["high"] >= df["close"])).all()))

    logger.info("\n[2] 降级与熔断")
    check("坏源已被调用并降级到 mock", broken.calls >= 1, f"calls={broken.calls}")
    check("坏源失败计数累积", broken.health.consecutive_failures >= 1)
    hub.get_bars(syms[1], Freq.D1, "2026-01-05", "2026-06-30")
    check("达到阈值后熔断打开", broken.health.is_open, f"fails={broken.health.consecutive_failures}")
    calls_before = broken.calls
    hub.get_bars(syms[2], Freq.D1, "2026-01-05", "2026-06-30")
    check("熔断期间不再调用坏源", broken.calls == calls_before)
    snap = hub.health_snapshot()
    check("健康快照包含两个源", len(snap) == 2, str([s["name"] for s in snap]))

    logger.info("\n[3] 缓存")
    hub.cache.clear()
    hub.get_bars(syms[0], Freq.D1, "2026-01-05", "2026-06-30")
    stats1 = hub.cache.stats()
    hub.get_bars(syms[0], Freq.D1, "2026-01-05", "2026-06-30")
    stats2 = hub.cache.stats()
    check("第二次命中缓存", stats2["hits"] > stats1["hits"], f"{stats1['hits']}->{stats2['hits']}")
    ticks = hub.get_realtime(syms[:3])
    check("实时行情返回", len(ticks) == 3)
    stats3 = hub.cache.stats()
    hub.get_realtime(syms[:3])
    check("tick 不进缓存", hub.cache.stats()["hits"] == stats3["hits"])

    logger.info("\n[4] PIT 时间切片")
    hub.set_asof(date(2026, 3, 31))
    df_pit = hub.get_bars(syms[0], Freq.D1, "2026-01-05", "2026-06-30")
    check("未来数据被截断", df_pit["date"].max().date() <= date(2026, 3, 31),
          f"max={df_pit['date'].max()}")
    news = hub.get_news([syms[0]], limit=20)
    check("新闻 PIT 过滤", all(n.publish_time <= datetime(2026, 3, 31, 23, 59, 59) for n in news),
          f"n={len(news)}")
    fund = hub.get_latest_fundamentals(syms[:3])
    ok_ann = all(f.ann_date <= date(2026, 3, 31) for f in fund.values())
    check("财务按 ann_date 切片", ok_ann, f"n={len(fund)}")

    g = PITGuard(date(2026, 3, 31), strict=True)
    try:
        g.filter_frame(df.copy(), "date")
        raised = False
    except LookAheadError:
        raised = True
    check("strict 模式检测到未来数据抛错", raised)
    try:
        assert_no_lookahead(df_pit, date(2026, 3, 31), "date")
        clean = True
    except LookAheadError:
        clean = False
    check("切片后数据通过前视检查", clean)
    hub.set_asof(None)

    logger.info("\n[5] 数据质量")
    rep = hub.validate_bars(df)
    check("正常数据质量 OK", rep.ok, str(rep))
    bad = df.copy()
    bad.loc[bad.index[10], "close"] = bad.loc[bad.index[10], "close"] * 3
    rep2 = hub.validate_bars(bad)
    check("异常涨跌幅被识别", not rep2.ok, str(rep2)[:80])

    logger.info("\n[6] 极端场景注入")
    mock.inject(syms[3], "2026-06-10", "limit_up")
    mock.inject(syms[3], "2026-06-11", "limit_down")
    mock.inject(syms[3], "2026-06-12", "suspended")
    hub.cache.clear()
    d2 = hub.get_bars(syms[3], Freq.D1, "2026-06-05", "2026-06-20")
    d2 = d2.set_index(d2["date"].dt.date)
    up = d2.loc[date(2026, 6, 10)]
    down = d2.loc[date(2026, 6, 11)]
    check("涨停日 high==low==close", abs(up["high"] - up["close"]) < 1e-6 and abs(up["low"] - up["close"]) < 1e-6)
    check("跌停日收盘低于前日", down["close"] < up["close"])
    check("停牌日无成交量", date(2026, 6, 12) not in d2.index or d2.loc[date(2026, 6, 12), "volume"] == 0)

    logger.info("\n[7] 标的与其他数据")
    insts = hub.get_instruments(syms[:5])
    check("标的信息返回", len(insts) == 5)
    check("含行业字段", all(i.industry for i in insts))
    idx = hub.get_index_bars("000300.SH", "2026-01-05", "2026-06-30")
    check("指数行情返回", not idx.empty, f"rows={len(idx)}")
    events = hub.get_events([syms[0]])
    check("事件返回列表", isinstance(events, list))
    mf = hub.get_money_flow(syms[:3])
    check("资金流返回", mf is not None)

    logger.info("\n[8] 全源失败 fail-safe")
    hub2 = DataHub(st, [BrokenProvider()])
    try:
        hub2.get_bars(syms[0], Freq.D1, "2026-01-05", "2026-06-30")
        raised = False
    except DataUnavailableError:
        raised = True
    check("全部失败抛 DataUnavailableError(fail-safe)", raised)

    logger.info(f"\n{'=' * 46}\n通过 {PASS} / 失败 {FAIL}\n{'=' * 46}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())