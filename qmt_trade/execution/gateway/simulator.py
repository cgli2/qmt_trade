"""回测 / 模拟盘网关（设计 6.8.1 P7）。

撮合规则（保守、避免未来函数）：
- 订单在 T 日收盘后生成，于 **T+1 日开盘**撮合（由 backtest 引擎负责把 market_day 设为 T+1）。
- 买入限价单：仅当当日最低价触及限价（bar.low <= price）才成交，成交价取限价（更优则不追）。
- 卖出限价单：仅当当日最高价触及限价成交。
- 市价单：直接按开盘价成交。
- 滑点由 CostModel 统一施加（买入偏贵、卖出偏便宜）。

不模拟盘中分时，是 M1 合理简化；M3 接入真实 Tick 后可换更细的撮合。
"""

from __future__ import annotations

from datetime import date, datetime

from ...core.trading import Fill, Order, Side
from ...datahub.types import Bar
from ..costs import CostModel
from .base import Gateway


class SimGateway(Gateway):
    def __init__(self) -> None:
        self.fill_seq = 0

    def is_connected(self) -> bool:
        return True

    def submit(self, order: Order, market_day: date, bar: Bar | None,
               cost: CostModel) -> Fill | None:
        if bar is None:
            return None
        side = order.side
        ref = bar.open if bar.open and bar.open > 0 else bar.close
        limit = order.price

        if order.order_type.value == "LIMIT" and limit is not None:
            if side is Side.BUY and bar.low > limit + 1e-6:
                return None  # 当日未触达限价，不成交
            if side is Side.SELL and bar.high < limit - 1e-6:
                return None
            # 限价单以限价成交（更优则不追高）
            ref = limit

        fill_price = cost.fill_price(side, ref, volume_ratio=0.0)
        self.fill_seq += 1
        ts = datetime.combine(market_day, datetime.min.time().replace(hour=9, minute=31))
        return Fill(
            fill_id=f"sim_{market_day.isoformat()}_{self.fill_seq}",
            order_id=order.order_id,
            symbol=order.symbol,
            side=side,
            quantity=order.quantity,
            price=fill_price,
            commission=cost.commission(order.quantity * fill_price),
            stamp_tax=cost.stamp_tax(order.quantity * fill_price) if side is Side.SELL else 0.0,
            transfer_fee=cost.transfer(order.quantity * fill_price),
            timestamp=ts,
        )
