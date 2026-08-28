"""数据层通用类型。

所有带时间属性的记录都必须有 ``publish_time``/``ann_time``，这是 PIT 的前提。
设计文档 6.1.1 特别强调：财务数据必须按**公告日**而非报告期入库，否则回测必然穿越。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class Freq(str, Enum):
    D1 = "1d"
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    M60 = "60m"


class Adjust(str, Enum):
    NONE = "none"
    QFQ = "qfq"   # 前复权：展示用
    HFQ = "hfq"   # 后复权：回测用（历史价格不会因除权而变动")


class SourceSkipped(Exception):
    """数据源主动声明「本次我不服务，请降级到下一个源」。

    与抛普通 Exception 的区别：``DataHub._dispatch`` 捕获它时**只跳过该源、
    不记健康度失败、不触发熔断**——否则 QMT 财务下载超时会被误判成 QMT 整体
    故障，把同样健康的 QMT 行情一并熔断。用于「该源在此条件下不可用，但其它
    源可能可用」的场景（如 QMT 财务数据未授权 → 降级 akshare）。
    """


# 标准 K 线列，所有 provider 必须对齐到这套列名
BAR_COLUMNS = [
    "date", "symbol", "open", "high", "low", "close", "volume", "amount",
    "prev_close", "limit_up", "limit_down", "is_suspended", "turnover_rate",
]


@dataclass(frozen=True)
class Bar:
    date: date
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0
    prev_close: float = 0.0
    turnover_rate: float = 0.0
    is_suspended: bool = False
    limit_up: float | None = None
    limit_down: float | None = None


@dataclass(frozen=True)
class Tick:
    symbol: str
    time: datetime
    last: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    bid_prices: tuple[float, ...] = ()
    bid_volumes: tuple[float, ...] = ()
    ask_prices: tuple[float, ...] = ()
    ask_volumes: tuple[float, ...] = ()

    @property
    def bid1(self) -> float:
        return self.bid_prices[0] if self.bid_prices else self.last

    @property
    def ask1(self) -> float:
        return self.ask_prices[0] if self.ask_prices else self.last

    @property
    def is_limit_locked_up(self) -> bool:
        """一字/封死涨停：无卖盘。买不进。"""
        return bool(self.ask_prices) and self.ask_prices[0] <= 0

    @property
    def is_limit_locked_down(self) -> bool:
        """封死跌停：无买盘。卖不出。"""
        return bool(self.bid_prices) and self.bid_prices[0] <= 0


@dataclass
class NewsItem:
    id: str
    title: str
    publish_time: datetime
    symbol: str | None = None
    content: str = ""
    source: str = ""
    url: str = ""
    category: str = ""
    importance: float = 0.0
    sentiment: float | None = None

    def to_row(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "url": self.url,
            "publish_time": self.publish_time.timestamp(),
            "category": self.category,
            "importance": self.importance,
            "sentiment": self.sentiment,
        }


class EventCategory(str, Enum):
    EARNINGS_FORECAST = "EARNINGS_FORECAST"      # 业绩预告
    EARNINGS_REPORT = "EARNINGS_REPORT"          # 定期报告
    RESTRUCTURING = "RESTRUCTURING"              # 重组
    SHARE_REDUCTION = "SHARE_REDUCTION"          # 减持
    INVESTIGATION = "INVESTIGATION"              # 立案调查（确定性利空）
    REGULATORY_PENALTY = "REGULATORY_PENALTY"    # 监管处罚
    SUSPENSION = "SUSPENSION"                    # 停牌
    UNLOCK = "UNLOCK"                            # 解禁
    DIVIDEND = "DIVIDEND"
    CONTRACT = "CONTRACT"                        # 重大合同
    POLICY = "POLICY"                            # 行业政策
    OTHER = "OTHER"


#: 规则先行的确定性利空事件——不等 LLM，直接减仓（设计 6.5.2）
HARD_NEGATIVE_EVENTS = {
    EventCategory.INVESTIGATION,
    EventCategory.REGULATORY_PENALTY,
}


@dataclass
class CorpEvent:
    id: str
    symbol: str
    category: EventCategory
    title: str
    ann_time: datetime
    detail: str = ""
    importance: float = 0.5
    sentiment: float = 0.0

    @property
    def publish_time(self) -> datetime:
        return self.ann_time

    @property
    def is_hard_negative(self) -> bool:
        return self.category in HARD_NEGATIVE_EVENTS


@dataclass
class Fundamental:
    symbol: str
    ann_date: date            # ★ 公告日，PIT 的判定依据
    report_period: date       # 报告期
    revenue: float = 0.0
    net_profit: float = 0.0
    roe: float = 0.0
    revenue_yoy: float = 0.0
    profit_yoy: float = 0.0
    gross_margin: float = 0.0
    debt_ratio: float = 0.0
    ocf: float = 0.0
    eps: float = 0.0
    bps: float = 0.0

    @property
    def publish_time(self) -> datetime:
        return datetime.combine(self.ann_date, datetime.min.time())


@dataclass
class InstrumentInfo:
    symbol: str
    name: str = ""
    industry: str = ""
    list_date: date | None = None
    total_share: float = 0.0
    float_share: float = 0.0
    is_st: bool = False
    is_suspended: bool = False
    market_cap: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def list_days(self, asof: date) -> int:
        if not self.list_date:
            return 10_000
        return (asof - self.list_date).days
