"""Tushare 数据源：财务、股本、指数、龙虎榜。

依赖与 token 都是**软依赖**：未安装或未配置时 :meth:`is_available` 返回 False，
DataHub 会自动跳到下一个源，而不是抛异常打断流程。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Sequence

import pandas as pd

from ...core.logging import get_logger
from ..types import Adjust, Freq, Fundamental, InstrumentInfo
from .base import Capability, DataProvider

logger = get_logger("datahub.tushare")


def _ts_code(symbol: str) -> str:
    return symbol.upper()


def _fmt(d: date | str | None) -> str | None:
    if d is None:
        return None
    if isinstance(d, str):
        return d.replace("-", "")[:8]
    return d.strftime("%Y%m%d")


class TushareProvider(DataProvider):
    name = "tushare"
    capabilities = {
        Capability.BARS,
        Capability.FUNDAMENTALS,
        Capability.INSTRUMENTS,
        Capability.INDEX,
    }

    def __init__(self, token: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.token = token or os.environ.get("TUSHARE_TOKEN", "")
        self._pro = None

    def is_available(self) -> bool:
        if not self.token:
            return False
        try:
            import tushare  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def pro(self):
        if self._pro is None:
            import tushare as ts

            ts.set_token(self.token)
            self._pro = ts.pro_api()
        return self._pro

    # ------------------------------------------------------------ 行情
    def get_bars(
        self,
        symbols: Sequence[str],
        freq: Freq = Freq.D1,
        start: date | str | None = None,
        end: date | str | None = None,
        adjust: Adjust = Adjust.HFQ,
    ) -> pd.DataFrame:
        if freq != Freq.D1:
            raise NotImplementedError("Tushare 源本项目只用日线")
        import tushare as ts

        frames = []
        adj = {Adjust.HFQ: "hfq", Adjust.QFQ: "qfq", Adjust.NONE: None}[adjust]
        for sym in symbols:
            df = ts.pro_bar(
                ts_code=_ts_code(sym), adj=adj, start_date=_fmt(start), end_date=_fmt(end), freq="D"
            )
            if df is None or df.empty:
                continue
            df = df.rename(
                columns={
                    "trade_date": "date",
                    "ts_code": "symbol",
                    "vol": "volume",
                    "pre_close": "prev_close",
                }
            )
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
            df["volume"] = df["volume"] * 100.0     # 手 → 股
            df["amount"] = df["amount"] * 1000.0    # 千元 → 元
            df["is_suspended"] = False
            frames.append(
                df[["date", "symbol", "open", "high", "low", "close", "volume", "amount",
                    "prev_close", "is_suspended"]]
            )
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)

    def get_index_bars(
        self, index_symbol: str, start: date | str | None = None, end: date | str | None = None
    ) -> pd.DataFrame:
        df = self.pro.index_daily(
            ts_code=_ts_code(index_symbol), start_date=_fmt(start), end_date=_fmt(end)
        )
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"trade_date": "date", "ts_code": "symbol", "vol": "volume",
                                "pre_close": "prev_close"})
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        return df.sort_values("date").reset_index(drop=True)

    # ------------------------------------------------------------ 基础信息
    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[InstrumentInfo]:
        basic = self.pro.stock_basic(
            exchange="", list_status="L",
            fields="ts_code,name,industry,list_date,market",
        )
        if basic is None or basic.empty:
            return []
        wanted = {s.upper() for s in symbols} if symbols else None
        out: list[InstrumentInfo] = []
        for row in basic.itertuples(index=False):
            code = str(row.ts_code)
            if wanted and code not in wanted:
                continue
            name = str(row.name)
            out.append(
                InstrumentInfo(
                    symbol=code,
                    name=name,
                    industry=str(getattr(row, "industry", "") or ""),
                    list_date=datetime.strptime(str(row.list_date), "%Y%m%d").date()
                    if getattr(row, "list_date", None)
                    else None,
                    is_st="ST" in name.upper(),
                )
            )
        return out

    # ------------------------------------------------------------ 财务（PIT 关键）
    def get_fundamentals(
        self, symbols: Sequence[str], start: date | str | None = None, end: date | str | None = None
    ) -> list[Fundamental]:
        out: list[Fundamental] = []
        for sym in symbols:
            df = self.pro.fina_indicator(
                ts_code=_ts_code(sym), start_date=_fmt(start), end_date=_fmt(end),
                fields="ts_code,ann_date,end_date,roe,or_yoy,netprofit_yoy,grossprofit_margin,debt_to_assets,eps,bps",
            )
            if df is None or df.empty:
                continue
            for row in df.itertuples(index=False):
                ann = getattr(row, "ann_date", None)
                period = getattr(row, "end_date", None)
                if not ann or not period:
                    # ★ 没有公告日的记录一律丢弃：宁可少数据，也不能拿报告期当公告日用
                    continue
                out.append(
                    Fundamental(
                        symbol=str(row.ts_code),
                        ann_date=datetime.strptime(str(ann), "%Y%m%d").date(),
                        report_period=datetime.strptime(str(period), "%Y%m%d").date(),
                        roe=_f(getattr(row, "roe", 0)) / 100.0,
                        revenue_yoy=_f(getattr(row, "or_yoy", 0)) / 100.0,
                        profit_yoy=_f(getattr(row, "netprofit_yoy", 0)) / 100.0,
                        gross_margin=_f(getattr(row, "grossprofit_margin", 0)) / 100.0,
                        debt_ratio=_f(getattr(row, "debt_to_assets", 0)) / 100.0,
                        eps=_f(getattr(row, "eps", 0)),
                        bps=_f(getattr(row, "bps", 0)),
                    )
                )
        return out


def _f(v) -> float:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0
