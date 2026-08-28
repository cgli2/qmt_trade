"""执行网关抽象（设计 6.8.1 / P7）。

回测与实盘**共用** ``ExecutionService`` 和 ``OrderGuard``，只在最底层换网关。
SimGateway 用于回测/模拟，QMTGateway 用于实盘。本抽象确保上层代码路径
完全一致 —— 从源头杜绝 qmt_etf "回测不算滑点、实盘算" 的失真。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ...core.trading import Fill, Order
from ...datahub.types import Bar
from ..costs import CostModel


class Gateway(ABC):
    """把一笔订单在某个交易日转成成交回报。"""

    @abstractmethod
    def submit(self, order: Order, market_day: date, bar: Bar | None,
               cost: CostModel) -> Fill | None:
        """返回成交（部分成交也用单笔 Fill 表达，remaining 由 Order 状态体现），
        无法成交返回 None（如限价单当日未触达）。"""
        raise NotImplementedError

    @abstractmethod
    def is_connected(self) -> bool:
        raise NotImplementedError
