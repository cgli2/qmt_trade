"""数据源抽象与健康度管理。

借鉴 TradingAgents-CN 的 ``data_source_manager.py``（优先级 + 自动降级），
但补上它没有的东西：**健康度统计与熔断**。原实现每次都从头试一遍主源，
主源挂了的那几分钟里每次请求都要先超时一次，延迟被放大。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Sequence

import pandas as pd

from ...core.logging import get_logger
from ..types import Adjust, CorpEvent, Freq, Fundamental, InstrumentInfo, NewsItem, Tick

logger = get_logger("datahub.provider")


class Capability(str, Enum):
    BARS = "bars"
    TICK = "tick"
    FUNDAMENTALS = "fundamentals"
    INSTRUMENTS = "instruments"
    NEWS = "news"
    EVENTS = "events"
    INDEX = "index"
    MONEY_FLOW = "money_flow"


@dataclass
class ProviderHealth:
    """数据源健康度 + 熔断器。"""

    name: str
    fail_threshold: int = 3
    cooldown_seconds: float = 300.0
    consecutive_failures: int = 0
    total_calls: int = 0
    total_failures: int = 0
    total_latency: float = 0.0
    open_until: float = 0.0
    last_error: str = ""
    history: list[bool] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        """熔断是否处于打开状态（打开 = 暂时不用这个源）。"""
        return time.time() < self.open_until

    @property
    def success_rate(self) -> float:
        if not self.total_calls:
            return 1.0
        return 1.0 - self.total_failures / self.total_calls

    @property
    def avg_latency_ms(self) -> float:
        ok = self.total_calls - self.total_failures
        return round(self.total_latency / ok * 1000, 1) if ok else 0.0

    def record_success(self, latency: float) -> None:
        self.total_calls += 1
        self.total_latency += latency
        self.consecutive_failures = 0
        self.open_until = 0.0
        self._push(True)

    def record_failure(self, error: str) -> None:
        self.total_calls += 1
        self.total_failures += 1
        self.consecutive_failures += 1
        self.last_error = error
        self._push(False)
        if self.consecutive_failures >= self.fail_threshold:
            self.open_until = time.time() + self.cooldown_seconds
            logger.warning(
                "数据源 %s 连续失败 %d 次，熔断 %.0f 秒。最后错误: %s",
                self.name, self.consecutive_failures, self.cooldown_seconds, error,
            )

    def _push(self, ok: bool) -> None:
        self.history.append(ok)
        if len(self.history) > 100:
            del self.history[:-100]

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "healthy": not self.is_open,
            "success_rate": round(self.success_rate, 4),
            "avg_latency_ms": self.avg_latency_ms,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }


class DataProvider(ABC):
    """数据源基类。子类只需实现自己支持的方法，能力由 :attr:`capabilities` 声明。"""

    name: str = "base"
    capabilities: set[Capability] = set()

    def __init__(self, **kwargs):
        self.options = kwargs
        self.health = ProviderHealth(
            name=self.name,
            fail_threshold=int(kwargs.get("fail_threshold", 3)),
            cooldown_seconds=float(kwargs.get("cooldown_seconds", 300)),
        )

    def supports(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def is_available(self) -> bool:
        """依赖是否就绪（如第三方 SDK 是否装了、token 是否配了）。"""
        return True

    # ------------------------------------------------------------ 数据接口
    def get_bars(
        self,
        symbols: Sequence[str],
        freq: Freq = Freq.D1,
        start: date | str | None = None,
        end: date | str | None = None,
        adjust: Adjust = Adjust.HFQ,
    ) -> pd.DataFrame:
        raise NotImplementedError(f"{self.name} 不支持 get_bars")

    def get_realtime(self, symbols: Sequence[str]) -> dict[str, Tick]:
        raise NotImplementedError(f"{self.name} 不支持 get_realtime")

    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[InstrumentInfo]:
        raise NotImplementedError(f"{self.name} 不支持 get_instruments")

    def get_fundamentals(
        self, symbols: Sequence[str], start: date | str | None = None, end: date | str | None = None
    ) -> list[Fundamental]:
        raise NotImplementedError(f"{self.name} 不支持 get_fundamentals")

    def get_news(
        self,
        symbols: Sequence[str] | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 200,
    ) -> list[NewsItem]:
        raise NotImplementedError(f"{self.name} 不支持 get_news")

    def get_events(
        self,
        symbols: Sequence[str] | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> list[CorpEvent]:
        raise NotImplementedError(f"{self.name} 不支持 get_events")

    def get_money_flow(
        self, symbols: Sequence[str], start: date | str | None = None, end: date | str | None = None
    ) -> pd.DataFrame:
        raise NotImplementedError(f"{self.name} 不支持 get_money_flow")

    def get_index_bars(
        self, index_symbol: str, start: date | str | None = None, end: date | str | None = None
    ) -> pd.DataFrame:
        raise NotImplementedError(f"{self.name} 不支持 get_index_bars")

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        pass

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} name={self.name}>"
