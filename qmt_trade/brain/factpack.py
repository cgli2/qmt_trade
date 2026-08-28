"""FactPack Builder（设计 6.4.2，非 LLM）。

把结构化数据整理成"事实卡片"喂给 LLM，而不是让 LLM 自己调工具去算——
这是相对 TradingAgents-CN 的重要增量，能大幅降低幻觉与工具调用错误：

- 所有数字**预先算好**，并显式标注数据日期（PIT 追溯）；
- 缺失字段显式列出「不得臆测」，而不是留空让模型脑补；
- 同时构建 ``numerics`` 索引，供事实校验器把 LLM 输出里的数字逐一比对。
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any

import pandas as pd

from .state import Fact, FactPack

logger = logging.getLogger(__name__)

#: 送进 FactPack 的因子白名单：(列名, 展示名, 单位)
_FACTOR_FIELDS: list[tuple[str, str, str]] = [
    ("close", "最新收盘价", "元"),
    ("turnover_rate", "换手率", ""),
    ("ret_20d", "20日涨跌幅", ""),
    ("ret_60d", "60日涨跌幅", ""),
    ("mom_12_1", "12-1月动量", ""),
    ("bias_20", "20日乖离率", ""),
    ("ma_align", "均线多头排列分", ""),
    ("breakout_60", "距60日高点比", ""),
    ("atr_ratio", "ATR占价比", ""),
    ("downside_vol", "下行波动率", ""),
    ("max_drawdown_60", "60日最大回撤", ""),
    ("roe", "ROE", ""),
    ("gross_margin", "毛利率", ""),
    ("profit_yoy", "净利同比", ""),
    ("revenue_yoy", "营收同比", ""),
    ("earnings_yield", "盈利收益率(E/P)", ""),
    ("book_to_price", "市净率倒数(B/P)", ""),
    ("debt_safety", "偿债安全分", ""),
    ("main_net_5d", "近5日主力净流入", "元"),
    ("main_net_10d", "近10日主力净流入", "元"),
    ("main_net_ratio", "主力净流入占成交比", ""),
    ("large_order_ratio", "大单占比", ""),
    ("flow_consistency", "资金流一致性", ""),
    ("news_sentiment_5d", "近5日新闻情绪", ""),
    ("news_heat_5d", "近5日新闻热度", ""),
    ("event_sentiment_20d", "近20日事件情绪", ""),
    ("industry_momentum", "所属行业动量", ""),
]

#: 分位列（LLM 更容易理解"在全市场排第几"而不是绝对值）
_PCTL_FIELDS: list[tuple[str, str]] = [
    ("cat_momentum", "动量类分位"),
    ("cat_moneyflow", "资金类分位"),
    ("cat_fundamental", "基本面类分位"),
    ("cat_sentiment", "情绪类分位"),
    ("cat_quality", "质量类分位"),
]


def _clean(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (pd.Timestamp,)):
        return v.date()
    return v


class FactPackBuilder:
    """从选股 frame + DataHub 构造 FactPack。

    Parameters
    ----------
    hub : DataHub
        用于补充新闻与事件的原文摘要（有则加，无则跳过，绝不阻塞主流程）。
    news_days : int
        回看多少个自然日的新闻/事件。
    """

    def __init__(self, hub=None, *, news_days: int = 7, max_news: int = 5):
        self.hub = hub
        self.news_days = news_days
        self.max_news = max_news

    def build(self, symbol: str, row: pd.Series | dict, asof: date, *,
              name: str = "", extra: dict[str, Any] | None = None) -> FactPack:
        row = dict(row) if not isinstance(row, dict) else row
        industry = str(row.get("industry") or "")
        # 数据基准日：frame 里的 date 列就是 T-1（PIT 纪律的体现）
        data_day = _clean(row.get("date")) or asof
        if isinstance(data_day, pd.Timestamp):
            data_day = data_day.date()

        fp = FactPack(symbol=symbol, asof=asof, industry=industry, name=name)

        fp.add("综合分", _clean(row.get("score")), asof=data_day, source="factor")
        fp.add("综合分市场分位", _clean(row.get("_pct")), asof=data_day, source="factor")
        fp.add("候选池排名", _clean(row.get("rank")), asof=data_day, source="factor")

        for col, label, unit in _FACTOR_FIELDS:
            fp.add(label, _clean(row.get(col)), asof=data_day, source="factor", unit=unit)
        for col, label in _PCTL_FIELDS:
            fp.add(label, _clean(row.get(col)), asof=data_day, source="factor")

        # 硬负面事件是硬闸，必须显式呈现
        hn = _clean(row.get("hard_negative_flag"))
        if hn is not None:
            fp.add("硬负面事件标记", float(hn), asof=data_day, source="event")

        for k, v in (extra or {}).items():
            fp.add(k, _clean(v), asof=asof, source="ctx")

        self._attach_news(fp, symbol, asof)
        return fp

    # ------------------------------------------------------------- 新闻/事件
    def _attach_news(self, fp: FactPack, symbol: str, asof: date) -> None:
        if self.hub is None:
            return
        start = asof - timedelta(days=self.news_days)
        try:
            news = self.hub.get_news([symbol], start, asof)
        except Exception as exc:  # 新闻是增强项，失败不阻塞
            logger.debug("FactPack 取新闻失败 %s: %s", symbol, exc)
            news = None
        if news is not None and len(news):
            df = news if isinstance(news, pd.DataFrame) else pd.DataFrame(news)
            for i, (_, r) in enumerate(df.tail(self.max_news).iterrows()):
                title = str(r.get("title") or r.get("summary") or "")[:80]
                if not title:
                    continue
                d = _clean(r.get("datetime") or r.get("date"))
                if isinstance(d, pd.Timestamp):
                    d = d.date()
                fp.facts.append(Fact(f"新闻{i + 1}", title, d or asof, "news", ""))
        try:
            events = self.hub.get_events([symbol], start, asof)
        except Exception as exc:
            logger.debug("FactPack 取事件失败 %s: %s", symbol, exc)
            events = None
        if events is not None and len(events):
            df = events if isinstance(events, pd.DataFrame) else pd.DataFrame(events)
            kinds = [str(x) for x in df.get("event_type", pd.Series(dtype=str)).tolist()[:5]]
            if kinds:
                fp.add("近期事件类型", ",".join(kinds), asof=asof, source="event")


def build_factpacks(frame: pd.DataFrame, asof: date, symbols: list[str],
                    *, hub=None, extra: dict[str, dict] | None = None) -> dict[str, FactPack]:
    """批量构造。frame 需含 ``symbol`` 列。"""
    builder = FactPackBuilder(hub)
    out: dict[str, FactPack] = {}
    if frame is None or frame.empty:
        return out
    idx = frame.set_index("symbol", drop=False)
    for s in symbols:
        if s not in idx.index:
            continue
        row = idx.loc[s]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        out[s] = builder.build(s, row, asof, extra=(extra or {}).get(s))
    return out
