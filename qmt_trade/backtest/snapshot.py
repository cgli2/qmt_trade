"""Frozen backtest inputs. Only the owner downloads; workers have no providers.

The pickle metadata is generated locally by the owner, stored in a dedicated
temporary DuckDB, and hash-verified before worker startup. Never import a
user-supplied pickle or snapshot through this interface.
"""
import pickle
from datetime import date, timedelta

import pandas as pd

from ..datahub.providers.base import Capability, DataProvider
from ..datahub.types import Adjust, Freq
from ..storage.db import Database
from ..storage.market import MarketRepository


def prepare_snapshot(hub, settings, spec, path, progress, cancelled):
    from ..core.strategies import STANDALONE_STRATEGIES
    sid = spec["strategy"]
    start, end = date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"])
    # Existing factor engine requests 250 calendar days. Extend for trading-day
    # warmup and large strategy moving averages; never shorten existing inputs.
    warmup = max(550, int(spec.get("warmup", 250)))
    begin = start - timedelta(days=warmup)
    cfg = settings.section(f"strategies.{sid}")
    symbols = cfg.get("symbols") if sid in {"etf_t0", "stock_t0"} else None
    infos = hub.get_instruments(symbols)
    syms = list(symbols or [i.symbol for i in infos])
    if not syms:
        raise ValueError("没有可回测标的，请检查策略标的配置和数据源")
    db = Database(path, schema="snapshot")
    store = MarketRepository(db)
    from ..storage.industry import industry_map
    metadata = {"industry_map": industry_map(), "instruments": infos, "sources": list(hub.providers), "coverage": [], "limitations": []}
    try:
        adjustment = Adjust.NONE if sid in STANDALONE_STRATEGIES else Adjust.HFQ
        frequencies = [Freq.D1]
        if sid in {"etf_t0", "stock_t0"}:
            frequencies.append(Freq.M1)
        elif sid == "tail_pick":
            frequencies.append(Freq.M5)
        for freq in frequencies:
            for offset in range(0, len(syms), 50):
                if cancelled():
                    raise InterruptedError("任务已取消")
                batch = syms[offset:offset + 50]
                progress(f"准备快照 {freq.value}：{min(offset + 50, len(syms))}/{len(syms)} 个标的")
                frame = hub.get_bars(batch, freq, begin if freq == Freq.D1 else start, end, adjustment, validate=True)
                if frame is None or frame.empty:
                    raise ValueError(f"数据缺失：{freq.value} {batch[0]}… {start} 至 {end}")
                for symbol, part in frame.groupby("symbol"):
                    store.write("bars_" + freq.value + "_" + adjustment.value, part, symbol)
                metadata["coverage"].append({"frequency": freq.value, "symbols": batch, "rows": len(frame),
                                              "first": str(frame.date.min()), "last": str(frame.date.max())})
        for index in ("000300.SH", "000001.SH", "399006.SZ"):
            if cancelled():
                raise InterruptedError("任务已取消")
            frame = hub.get_index_bars(index, begin, end)
            if frame is not None and not frame.empty:
                store.write("index", frame, index)
        if sid not in STANDALONE_STRATEGIES:
            for name, fn in {
                "fundamentals": lambda: hub.get_fundamentals(syms, end=end),
                "news": lambda: hub.get_news(syms, begin, end, limit=20000),
                "events": lambda: hub.get_events(syms, begin, end),
            }.items():
                if cancelled():
                    raise InterruptedError("任务已取消")
                metadata[name] = fn()
            flow = hub.get_money_flow(syms, begin, end)
            if flow is not None and not flow.empty:
                for symbol, part in flow.groupby("symbol"):
                    store.write("moneyflow", part, symbol)
        db.execute("CREATE TABLE metadata (payload BLOB)")
        db.execute("INSERT INTO metadata VALUES (?)", [pickle.dumps(metadata, protocol=5)])
    finally:
        db.close()
    return metadata


class SnapshotProvider(DataProvider):
    name = "snapshot"
    capabilities = {Capability.BARS, Capability.INDEX, Capability.INSTRUMENTS,
                    Capability.FUNDAMENTALS, Capability.NEWS, Capability.EVENTS, Capability.MONEY_FLOW}

    def __init__(self, path):
        super().__init__()
        self.db = Database(path, schema="snapshot")
        self.store = MarketRepository(self.db)
        self.metadata = pickle.loads(self.db.scalar("SELECT payload FROM metadata"))

    def get_bars(self, symbols, freq=Freq.D1, start=None, end=None, adjust=Adjust.HFQ):
        if end is not None and freq != Freq.D1:
            end = pd.Timestamp(end).normalize() + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        return self.store.read_many("bars_" + freq.value + "_" + adjust.value, symbols, start=start, end=end)

    def get_index_bars(self, index_symbol, start=None, end=None):
        return self.store.read("index", index_symbol, start=start, end=end)

    def get_instruments(self, symbols=None):
        return [i for i in self.metadata["instruments"] if symbols is None or i.symbol in symbols]

    def get_fundamentals(self, symbols, start=None, end=None):
        return [i for i in self.metadata.get("fundamentals", []) if i.symbol in symbols]

    def get_news(self, symbols=None, start=None, end=None, limit=200):
        return self._records("news", symbols, start, end)[:limit]

    def get_events(self, symbols=None, start=None, end=None):
        return self._records("events", symbols, start, end)

    def _records(self, name, symbols, start, end):
        return [i for i in self.metadata.get(name, []) if
                (symbols is None or i.symbol in symbols) and
                (start is None or pd.Timestamp(i.publish_time) >= pd.Timestamp(start)) and
                (end is None or pd.Timestamp(i.publish_time) <= pd.Timestamp(end))]

    def get_money_flow(self, symbols, start=None, end=None):
        return self.store.read_many("moneyflow", symbols, start=start, end=end)

    def close(self):
        self.db.close()
