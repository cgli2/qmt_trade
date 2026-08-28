"""交易底层原语：订单/成交/持仓/账户。

这些结构体是 risk / portfolio / execution / backtest 四层共用的"货币"。
刻意保持**纯数据 + 无 IO**，方便回测与实盘走同一套代码路径（P7）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    PENDING = "PENDING"      # 已生成，尚未提交网关
    SUBMITTED = "SUBMITTED"  # 已提交，等待成交
    PART_FILLED = "PART_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass
class Order:
    """一笔订单。``idempotency_key`` 是幂等键（见 OrderGuard）。"""
    order_id: str
    symbol: str
    side: Side
    quantity: int                # 股数（A股 100 的整数倍）
    price: float | None = None   # None = 市价
    order_type: OrderType = OrderType.LIMIT
    status: OrderStatus = OrderStatus.PENDING
    idempotency_key: str = ""
    created_at: datetime | None = None
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    rejected_reason: str = ""
    # 回测/实盘扩展字段
    note: str = ""

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PART_FILLED)

    @property
    def remaining(self) -> int:
        return max(0, self.quantity - self.filled_qty)


@dataclass
class Fill:
    """一笔成交回报。"""
    fill_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: float
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    timestamp: datetime | None = None

    @property
    def amount(self) -> float:
        return self.quantity * self.price

    @property
    def total_fee(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee


@dataclass
class Position:
    """单标的持仓。"""
    symbol: str
    shares: int
    avg_cost: float           # 持仓成本价（含买入费用摊销）
    can_use: int = 0           # 可卖数量（T+1）
    industry: str = ""
    #: 由 TradeIntent 带来的风控元数据（止损/止盈/失效条件），盘后守护用
    stop_loss_price: float | None = None
    stop_loss_type: str = ""
    take_profit: list[dict] = field(default_factory=list)
    invalidation_checks: list[str] = field(default_factory=list)
    max_holding_days: int = 0
    opened_at: date | None = None
    highest_since_open: float = 0.0   # 移动止盈用
    last_price: float = 0.0           # 最近一次实时/收盘现价（页面展示与浮盈口径）
    #: 分批止盈进度：已执行的档位序号（对应 take_profit 列表下标）
    tp_done_levels: list[int] = field(default_factory=list)
    #: 建仓时的原始股数，分批止盈按它乘比例，避免多次减仓后比例漂移
    origin_shares: int = 0

    @property
    def market_value(self) -> float:
        return self.shares * (self.last_price or self.avg_cost)

    def unrealized(self, last_price: float) -> float:
        return (last_price - self.avg_cost) * self.shares


@dataclass
class Account:
    """账户快照：总资产 = 可用现金 + 持仓市值。"""
    cash: float                      # 可用现金（已扣未成交冻结）
    positions: dict[str, Position] = field(default_factory=dict)
    frozen_cash: float = 0.0         # 挂单冻结资金
    total_asset: float = 0.0         # 缓存，由 PortfolioState 维护

    @property
    def position_value(self) -> float:
        return sum(p.shares * (p.last_price or p.avg_cost) for p in self.positions.values())
