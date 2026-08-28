"""确定性合成数据源。

作用有三个，都很实在：
1. **离线可测**：没有 QMT、没有 Tushare token 也能跑通全链路和回测；
2. **回归基线**：种子固定，同样的代码永远得到同样的回测结果，改动引入的偏差一眼可见；
3. **压力构造**：可以显式注入涨停、跌停、停牌、黑天鹅公告，用于验证风控分支。

生成模型：带行业共同因子的几何布朗运动 + 少量跳跃，足以驱动因子与风控逻辑，
但**不假装它有 alpha** —— 用合成数据跑出来的收益没有任何参考意义，只验证工程正确性。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from typing import Sequence

import numpy as np
import pandas as pd

from ...core.clock import TradingCalendar
from ...core.instruments import build_profile, normalize_symbol
from ..types import (
    Adjust,
    CorpEvent,
    EventCategory,
    Freq,
    Fundamental,
    InstrumentInfo,
    NewsItem,
    Tick,
)
from .base import Capability, DataProvider

INDUSTRIES = ["电子", "医药生物", "食品饮料", "银行", "电力设备", "机械设备", "计算机", "化工"]


def _seed_of(*parts) -> int:
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.md5(raw).hexdigest()[:8], 16)


class MockProvider(DataProvider):
    name = "mock"
    capabilities = {
        Capability.BARS,
        Capability.TICK,
        Capability.FUNDAMENTALS,
        Capability.INSTRUMENTS,
        Capability.NEWS,
        Capability.EVENTS,
        Capability.INDEX,
        Capability.MONEY_FLOW,
    }

    def __init__(
        self,
        n_symbols: int = 60,
        start: str = "2024-01-02",
        end: str = "2026-08-07",
        seed: int = 42,
        calendar: TradingCalendar | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.seed = seed
        self.calendar = calendar or TradingCalendar()
        self.start = date.fromisoformat(start)
        self.end = date.fromisoformat(end)
        self.dates = self.calendar.trading_days(self.start, self.end)
        self.symbols = self._make_symbols(n_symbols)
        self._instruments = self._make_instruments()
        self._bars: dict[str, pd.DataFrame] = {}
        self._index_cache: dict[str, pd.DataFrame] = {}
        #: 人工注入的极端场景 {(symbol, date): "limit_up"|"limit_down"|"suspended"}
        self.injections: dict[tuple[str, date], str] = {}

    # ------------------------------------------------------------ 构造
    def _make_symbols(self, n: int) -> list[str]:
        rng = np.random.default_rng(self.seed)
        pool: list[str] = []
        # 主板沪、主板深、创业板、科创板按 4:3:2:1 混合，保证覆盖各板块规则
        counts = [max(1, int(n * 0.4)), max(1, int(n * 0.3)), max(1, int(n * 0.2))]
        counts.append(max(1, n - sum(counts)))
        for prefix, market, cnt in (
            ("600", "SH", counts[0]),
            ("000", "SZ", counts[1]),
            ("300", "SZ", counts[2]),
            ("688", "SH", counts[3]),
        ):
            picked = rng.choice(np.arange(1, 900), size=cnt, replace=False)
            for i in sorted(picked):
                pool.append(f"{prefix}{int(i):03d}.{market}")
        return pool[:n]

    def _make_instruments(self) -> dict[str, InstrumentInfo]:
        out: dict[str, InstrumentInfo] = {}
        for idx, sym in enumerate(self.symbols):
            rng = np.random.default_rng(_seed_of(self.seed, sym, "inst"))
            list_offset = int(rng.integers(200, 3000))
            out[sym] = InstrumentInfo(
                symbol=sym,
                name=f"模拟{sym[:6]}",
                industry=INDUSTRIES[idx % len(INDUSTRIES)],
                list_date=self.start - timedelta(days=list_offset),
                total_share=float(rng.integers(2, 80)) * 1e8,
                float_share=float(rng.integers(1, 60)) * 1e8,
                is_st=False,
                is_suspended=False,
            )
        return out

    # ------------------------------------------------------------ 极端场景注入
    def inject(self, symbol: str, day: date | str, kind: str) -> None:
        """注入极端行情，用于测试风控分支。kind: limit_up / limit_down / suspended。"""
        d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
        self.injections[(normalize_symbol(symbol), d)] = kind
        self._bars.pop(normalize_symbol(symbol), None)

    # ------------------------------------------------------------ 行情生成
    def _gen_bars(self, symbol: str) -> pd.DataFrame:
        if symbol in self._bars:
            return self._bars[symbol]
        rng = np.random.default_rng(_seed_of(self.seed, symbol))
        n = len(self.dates)
        profile = build_profile(symbol)
        base_price = float(rng.uniform(6, 80))
        drift = float(rng.normal(0.0003, 0.0006))
        vol = float(rng.uniform(0.014, 0.032))

        # 行业共同因子：同行业股票有相关性，便于测试相关性约束
        industry = self._instruments[symbol].industry
        f_rng = np.random.default_rng(_seed_of(self.seed, industry, "factor"))
        common = f_rng.normal(0, 0.010, n)
        idio = rng.normal(0, 1.0, n) * vol
        rets = drift + 0.5 * common + idio
        # 少量跳跃
        jumps = rng.random(n) < 0.01
        rets[jumps] += rng.normal(0, 0.05, int(jumps.sum()))
        rets = np.clip(rets, -profile.limit_pct * 0.98, profile.limit_pct * 0.98)

        closes = base_price * np.exp(np.cumsum(rets))
        prev_closes = np.concatenate([[base_price], closes[:-1]])
        intraday = np.abs(rng.normal(0, 0.008, n)) + 0.002
        highs = np.maximum(closes, prev_closes) * (1 + intraday)
        lows = np.minimum(closes, prev_closes) * (1 - intraday)
        opens = prev_closes * (1 + rng.normal(0, 0.006, n))
        opens = np.clip(opens, lows, highs)
        float_share = self._instruments[symbol].float_share
        volumes = np.abs(rng.normal(1.2e7, 5e6, n)) + 1e6
        amounts = volumes * closes

        rows = pd.DataFrame(
            {
                "date": pd.to_datetime(self.dates),
                "symbol": symbol,
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
                "amount": amounts,
                "prev_close": prev_closes,
            }
        )
        rows["is_suspended"] = False
        rows["turnover_rate"] = rows["volume"] / max(float_share, 1.0)

        # 应用注入
        for i, d in enumerate(self.dates):
            kind = self.injections.get((symbol, d))
            if not kind:
                continue
            pc = float(rows.at[i, "prev_close"])
            if kind == "limit_up":
                lu = profile.limit_up(pc)
                rows.loc[i, ["open", "high", "low", "close"]] = lu
            elif kind == "limit_down":
                ld = profile.limit_down(pc)
                rows.loc[i, ["open", "high", "low", "close"]] = ld
            elif kind == "suspended":
                rows.loc[i, "is_suspended"] = True
                rows.loc[i, ["open", "high", "low", "close"]] = pc
                rows.loc[i, ["volume", "amount"]] = 0.0

        rows["limit_up"] = [profile.limit_up(pc) for pc in rows["prev_close"]]
        rows["limit_down"] = [profile.limit_down(pc) for pc in rows["prev_close"]]
        for col in ("open", "high", "low", "close", "prev_close", "limit_up", "limit_down"):
            rows[col] = rows[col].round(2)
        self._bars[symbol] = rows
        return rows

    # ------------------------------------------------------------ 接口实现
    def get_bars(
        self,
        symbols: Sequence[str],
        freq: Freq = Freq.D1,
        start: date | str | None = None,
        end: date | str | None = None,
        adjust: Adjust = Adjust.HFQ,
    ) -> pd.DataFrame:
        frames = []
        for sym in symbols:
            s = normalize_symbol(sym)
            if s not in self._instruments:
                continue
            frames.append(self._gen_bars(s))
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        if start:
            df = df.loc[df["date"] >= pd.Timestamp(start)]
        if end:
            df = df.loc[df["date"] <= pd.Timestamp(end)]
        return df.sort_values(["symbol", "date"]).reset_index(drop=True)

    def get_index_bars(
        self, index_symbol: str, start: date | str | None = None, end: date | str | None = None
    ) -> pd.DataFrame:
        if index_symbol not in self._index_cache:
            all_bars = self.get_bars(self.symbols)
            idx = (
                all_bars.groupby("date", as_index=False)
                .agg(close=("close", "mean"), volume=("volume", "sum"), amount=("amount", "sum"))
                .sort_values("date")
            )
            base = float(idx["close"].iloc[0])
            idx["close"] = idx["close"] / base * 3500.0
            idx["prev_close"] = idx["close"].shift(1).fillna(idx["close"].iloc[0])
            idx["open"] = idx["prev_close"]
            idx["high"] = idx[["open", "close"]].max(axis=1) * 1.004
            idx["low"] = idx[["open", "close"]].min(axis=1) * 0.996
            idx["symbol"] = index_symbol
            self._index_cache[index_symbol] = idx.reset_index(drop=True)
        df = self._index_cache[index_symbol]
        if start:
            df = df.loc[df["date"] >= pd.Timestamp(start)]
        if end:
            df = df.loc[df["date"] <= pd.Timestamp(end)]
        return df.reset_index(drop=True)

    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[InstrumentInfo]:
        syms = [normalize_symbol(s) for s in symbols] if symbols else self.symbols
        out = []
        for s in syms:
            info = self._instruments.get(s)
            if not info:
                continue
            bars = self._gen_bars(s)
            info.market_cap = float(bars["close"].iloc[-1]) * info.total_share
            out.append(info)
        return out

    def get_realtime(self, symbols: Sequence[str]) -> dict[str, Tick]:
        out: dict[str, Tick] = {}
        now = datetime.now()
        for sym in symbols:
            s = normalize_symbol(sym)
            bars = self._gen_bars(s)
            if bars.empty:
                continue
            last = bars.iloc[-1]
            price = float(last["close"])
            out[s] = Tick(
                symbol=s,
                time=now,
                last=price,
                open=float(last["open"]),
                high=float(last["high"]),
                low=float(last["low"]),
                prev_close=float(last["prev_close"]),
                volume=float(last["volume"]),
                amount=float(last["amount"]),
                bid_prices=(round(price * 0.999, 2),),
                bid_volumes=(10000.0,),
                ask_prices=(round(price * 1.001, 2),),
                ask_volumes=(10000.0,),
            )
        return out

    def get_fundamentals(
        self, symbols: Sequence[str], start: date | str | None = None, end: date | str | None = None
    ) -> list[Fundamental]:
        out: list[Fundamental] = []
        for sym in symbols:
            s = normalize_symbol(sym)
            if s not in self._instruments:
                continue
            rng = np.random.default_rng(_seed_of(self.seed, s, "fund"))
            for year in range(self.start.year, self.end.year + 1):
                for period_month, ann_offset in ((3, 30), (6, 60), (9, 30), (12, 110)):
                    period_end = date(year, period_month, 28)
                    ann = period_end + timedelta(days=ann_offset)
                    if ann > self.end + timedelta(days=200):
                        continue
                    out.append(
                        Fundamental(
                            symbol=s,
                            ann_date=ann,
                            report_period=period_end,
                            revenue=float(rng.uniform(1e8, 2e10)),
                            net_profit=float(rng.uniform(-2e8, 3e9)),
                            roe=float(rng.normal(0.09, 0.06)),
                            revenue_yoy=float(rng.normal(0.12, 0.25)),
                            profit_yoy=float(rng.normal(0.10, 0.40)),
                            gross_margin=float(np.clip(rng.normal(0.30, 0.12), 0.02, 0.9)),
                            debt_ratio=float(np.clip(rng.normal(0.45, 0.15), 0.05, 0.95)),
                            ocf=float(rng.uniform(-1e8, 2e9)),
                            eps=float(rng.normal(0.6, 0.5)),
                            bps=float(rng.uniform(2, 20)),
                        )
                    )
        return out

    def get_news(
        self,
        symbols: Sequence[str] | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 200,
    ) -> list[NewsItem]:
        syms = [normalize_symbol(s) for s in symbols] if symbols else self.symbols[:20]
        out: list[NewsItem] = []
        # 全区间均匀撒点：每只票平均每 12 个交易日一条，保证回测任意时点都有舆情可用
        n_per_symbol = max(3, len(self.dates) // 12)
        for sym in syms:
            rng = np.random.default_rng(_seed_of(self.seed, sym, "news"))
            picks = rng.choice(len(self.dates), size=min(n_per_symbol, len(self.dates)), replace=False)
            for k, di in enumerate(sorted(int(x) for x in picks)):
                # 盘前 9:00 或盘后 18:00，模拟真实发布时点分布
                hour = 9 if rng.random() < 0.5 else 18
                pub = datetime.combine(self.dates[di], datetime.min.time()) + timedelta(hours=hour)
                sentiment = float(np.clip(rng.normal(0, 0.4), -1, 1))
                out.append(
                    NewsItem(
                        id=f"news_{sym}_{k}",
                        title=f"{sym} 模拟新闻标题 {k}",
                        content=f"这是为测试生成的合成新闻，情感倾向 {sentiment:.2f}。",
                        publish_time=pub,
                        symbol=sym,
                        source="mock",
                        importance=float(rng.uniform(0, 1)),
                        sentiment=sentiment,
                    )
                )
        if start:
            out = [n for n in out if n.publish_time >= pd.Timestamp(start).to_pydatetime()]
        if end:
            out = [n for n in out if n.publish_time <= pd.Timestamp(end).to_pydatetime()]
        return sorted(out, key=lambda n: n.publish_time, reverse=True)[:limit]

    def get_events(
        self,
        symbols: Sequence[str] | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> list[CorpEvent]:
        syms = [normalize_symbol(s) for s in symbols] if symbols else self.symbols[:10]
        out: list[CorpEvent] = []
        cats = list(EventCategory)
        # 每只票全区间约 4 条公告，其中固定注入 1 条硬负面，用于验证 rule-first 强制规避
        for i, sym in enumerate(syms):
            rng = np.random.default_rng(_seed_of(self.seed, sym, "event"))
            picks = sorted(int(x) for x in rng.choice(len(self.dates), size=min(4, len(self.dates)), replace=False))
            for k, di in enumerate(picks):
                cat = cats[(i + k) % len(cats)]
                out.append(
                    CorpEvent(
                        id=f"evt_{sym}_{k}",
                        symbol=sym,
                        category=cat,
                        title=f"{sym} {cat.value} 模拟公告",
                        ann_time=datetime.combine(self.dates[di], datetime.min.time())
                        + timedelta(hours=18),
                        importance=float(rng.uniform(0.2, 1.0)),
                        sentiment=float(np.clip(rng.normal(0, 0.5), -1, 1)),
                    )
                )
            for (isym, iday), kind in self.injections.items():
                if isym == sym and kind in ("investigation", "penalty"):
                    out.append(
                        CorpEvent(
                            id=f"evt_{sym}_inject_{iday}",
                            symbol=sym,
                            category=EventCategory.INVESTIGATION
                            if kind == "investigation"
                            else EventCategory.REGULATORY_PENALTY,
                            title=f"{sym} 收到监管{'立案调查' if kind == 'investigation' else '处罚'}通知",
                            ann_time=datetime.combine(iday, datetime.min.time()) + timedelta(hours=18),
                            importance=1.0,
                            sentiment=-1.0,
                        )
                    )
        if start:
            out = [e for e in out if e.ann_time >= pd.Timestamp(start).to_pydatetime()]
        if end:
            out = [e for e in out if e.ann_time <= pd.Timestamp(end).to_pydatetime()]
        return out

    def get_money_flow(
        self, symbols: Sequence[str], start: date | str | None = None, end: date | str | None = None
    ) -> pd.DataFrame:
        frames = []
        for sym in symbols:
            s = normalize_symbol(sym)
            bars = self.get_bars([s], start=start, end=end)
            if bars.empty:
                continue
            rng = np.random.default_rng(_seed_of(self.seed, s, "flow"))
            ret = bars["close"].pct_change().fillna(0.0).to_numpy()
            # 资金流与当日涨跌弱相关 + 噪声，避免与动量因子完全共线
            net = (ret * 0.6 + rng.normal(0, 0.5, len(ret)) * 0.4) * bars["amount"].to_numpy() * 0.15
            frames.append(
                pd.DataFrame(
                    {
                        "date": bars["date"],
                        "symbol": s,
                        "net_inflow": net,
                        "big_order_ratio": np.clip(rng.normal(0.28, 0.08, len(ret)), 0.02, 0.85),
                    }
                )
            )
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
