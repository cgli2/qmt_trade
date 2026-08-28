"""组合状态：现金、持仓、成交回报的应用与盈亏核算。

是 risk / sizer / execution 共享的"账户真相"。回测与实盘的区别只在于
成交从哪来（SimGateway vs QMTGateway），但 **应用成交的逻辑完全相同**（P7）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..core.trading import Fill, Position, Side


@dataclass
class PortfolioState:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    initial_asset: float = 0.0
    #: 当日已实现盈亏（盘后清零）
    day_realized: float = 0.0
    #: 最近一次最大回撤（用于 Kill Switch 判定）
    peak_asset: float = 0.0
    #: 近 N 日总资产序列（回撤/连亏判定）
    equity_curve: list[float] = field(default_factory=list)
    #: 当日与近 5 日累计亏损
    day_loss: float = 0.0
    five_day_loss: float = 0.0
    _history_loss: list[float] = field(default_factory=list)
    #: 每笔卖出实现的盈亏（用于胜率统计）
    realized_log: list[float] = field(default_factory=list)
    #: 平仓明细（round-trip）。**L5 复盘归因的唯一数据源**——
    #: TradingAgents-CN 的复盘拿不到这层结构化明细，只能对着字符串报告空谈。
    closed_trades: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.initial_asset <= 0:
            self.initial_asset = self.cash
        if self.peak_asset <= 0:
            self.peak_asset = self.total_asset

    # -------------------------------------------------------- 查询
    @property
    def position_value(self) -> float:
        # 现价口径：无现价（回测未刷新/刚建仓）时兜底成本价，保证不低估现金占比
        return sum(p.shares * (p.last_price or p.avg_cost) for p in self.positions.values())

    @property
    def total_asset(self) -> float:
        return self.cash + self.position_value

    @property
    def max_drawdown(self) -> float:
        """自峰值以来的最大回撤（正数表示回撤幅度）。"""
        if self.peak_asset <= 0:
            return 0.0
        return max(0.0, (self.peak_asset - self.total_asset) / self.peak_asset)

    @property
    def day_pnl_pct(self) -> float:
        base = self.initial_asset or self.total_asset
        return (self.total_asset - base) / base if base else 0.0

    def position_weight(self, symbol: str) -> float:
        if not self.positions or self.total_asset <= 0:
            return 0.0
        p = self.positions.get(symbol)
        return (p.shares * (p.last_price or p.avg_cost) / self.total_asset) if p else 0.0

    def industry_weight(self, industry: str, sym_industry: dict[str, str]) -> float:
        if self.total_asset <= 0:
            return 0.0
        return sum(
            (p.shares * (p.last_price or p.avg_cost)) / self.total_asset
            for s, p in self.positions.items()
            if sym_industry.get(s) == industry
        )

    # -------------------------------------------------------- 成交应用
    def apply_fill(self, fill: Fill, cost_total: float, *, is_buy: bool,
                   signal: str = "", asof: date | None = None) -> None:
        """把一笔成交应用到账户。``cost_total`` 已含佣/税/过户。

        这是账户现金变动的唯一入口；挂单冻结等实时细节由 ExecutionService 另管，
        避免双重扣减。

        ``signal``/``asof`` 仅用于卖出时补全平仓明细（``closed_trades``），
        不参与任何金额计算。
        """
        if is_buy:
            pos = self.positions.get(fill.symbol)
            add_value = fill.amount + cost_total
            if pos is None:
                new_shares = fill.quantity
                new_avg = add_value / new_shares if new_shares else 0.0
                self.positions[fill.symbol] = Position(
                    symbol=fill.symbol, shares=new_shares, avg_cost=new_avg,
                    can_use=0, industry="", highest_since_open=new_avg,
                )
            else:
                total_shares = pos.shares + fill.quantity
                pos.avg_cost = (pos.shares * pos.avg_cost + add_value) / total_shares
                pos.shares = total_shares
            self.cash -= add_value
        else:
            pos = self.positions.get(fill.symbol)
            if pos is None:
                # 卖空不允许（A股）
                self.cash += fill.amount - cost_total
                return
            proceeds = fill.amount - cost_total
            self.cash += proceeds
            pnl = (fill.price - pos.avg_cost) * fill.quantity - cost_total
            self.day_realized += pnl
            self.realized_log.append(pnl)
            closed_at = asof or (fill.timestamp.date() if fill.timestamp else None)
            held = ((closed_at - pos.opened_at).days
                    if closed_at and pos.opened_at else 0)
            self.closed_trades.append({
                "symbol": fill.symbol,
                "entry_price": pos.avg_cost,
                "exit_price": fill.price,
                "shares": fill.quantity,
                "cost": cost_total,
                "pnl": pnl,
                "opened_at": pos.opened_at,
                "closed_at": closed_at,
                "holding_days": held,
                "industry": pos.industry,
                "reason": signal or "SIGNAL",
            })
            pos.shares -= fill.quantity
            pos.can_use = max(0, pos.can_use - fill.quantity)
            if pos.shares <= 0:
                self.positions.pop(fill.symbol, None)
        self._refresh_peak()

    def refresh(self, last_prices: dict[str, float]) -> None:
        """每个 bar 更新浮动盈亏相关缓存（现价、移动止盈最高点等）。"""
        for s, p in self.positions.items():
            lp = last_prices.get(s)
            if lp:
                p.last_price = lp
                p.highest_since_open = max(p.highest_since_open, lp)
        self._refresh_peak()

    def mark_t1(self, asof: date) -> None:
        """A股 T+1：标记已持有满 1 日的持仓为可卖。

        在回测/实盘每日开盘前调用，确保 Gate-1 的「可卖数量校验」能放行隔夜仓。
        """
        for p in self.positions.values():
            if p.opened_at and (asof - p.opened_at).days >= 1:
                p.can_use = p.shares

    def _refresh_peak(self) -> None:
        if self.total_asset > self.peak_asset:
            self.peak_asset = self.total_asset

    def record_equity(self, *, day_end: bool = False) -> None:
        """盘后记录权益并滚动亏损窗口。"""
        self.equity_curve.append(self.total_asset)
        pnl = self.total_asset - (self.equity_curve[-2] if len(self.equity_curve) > 1 else self.initial_asset)
        if pnl < 0:
            self._history_loss.append(pnl)
        else:
            self._history_loss.append(0.0)
        self._history_loss = self._history_loss[-5:]
        self.five_day_loss = abs(sum(self._history_loss))
        if day_end:
            self.day_loss = 0.0
