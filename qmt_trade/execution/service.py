"""执行服务（设计 6.8.1）：把交易意图落成成交并应用到组合。

编排顺序固定：**OrderGuard 防重/限频 → RiskEngine 前置闸门 → Gateway 撮合
→ 应用成交到 PortfolioState**。回测与实盘共用本类，仅 Gateway 不同（P7）。

LLM 无权直接调用本服务绕过风控：所有下单都必须先过 RiskEngine（P3）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..brain.schemas import TradeIntent
from ..core.trading import Fill, Order, OrderType, Side
from ..datahub.types import InstrumentInfo, Bar
from ..features.regime import RegimeSnapshot
from ..portfolio.sizer import PositionSizer, SizingContext
from ..portfolio.state import PortfolioState
from .costs import CostModel
from .gateway.base import Gateway
from .order_guard import OrderGuard
from ..risk.killswitch import KillSwitch
from ..risk.engine import RiskEngine

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    ok: bool
    symbol: str
    action: str
    shares: int = 0
    fill: Fill | None = None
    rejected_by: str = ""        # guard / risk / gateway
    reason: str = ""
    risk_violations: list[str] = field(default_factory=list)


class ExecutionService:
    def __init__(
        self,
        settings,
        gateway: Gateway,
        cost: CostModel,
        guard: OrderGuard,
        risk: RiskEngine,
        killswitch: KillSwitch,
        portfolio: PortfolioState,
        sizer: PositionSizer | None = None,
        repos=None,
    ):
        self.settings = settings
        self.gateway = gateway
        self.cost = cost
        self.guard = guard
        self.risk = risk
        self.killswitch = killswitch
        self.portfolio = portfolio
        self.sizer = sizer or PositionSizer(settings)
        self.repos = repos            # None = 回测等不落库场景
        #: 组合总风险预算（所有持仓止损空间之和占总资产上限）
        self.total_risk_budget = float(settings.get("portfolio.total_risk_budget", 0.03))

    # ----------------------------------------------------- 主入口
    def submit_intent(
        self,
        intent: TradeIntent,
        *,
        bar: Bar | None,
        market_day: date,
        asof: date,
        regime: RegimeSnapshot,
        instrument: InstrumentInfo | None,
        sym_industry: dict[str, str],
        daily_open_count: int = 0,
        high_corr_count: int = 0,
        plan_id: str = "plan",
        seq: int = 0,
        signal: str = "REBALANCE",
        reduce_shares: int = 0,
        exact_buy_shares: int = 0,
    ) -> ExecutionResult:
        res, meta = self._submit_core(
            intent, bar=bar, market_day=market_day, asof=asof, regime=regime,
            instrument=instrument, sym_industry=sym_industry,
            daily_open_count=daily_open_count, high_corr_count=high_corr_count,
            plan_id=plan_id, seq=seq, signal=signal, reduce_shares=reduce_shares,
            exact_buy_shares=exact_buy_shares)
        self._record_order(res, meta, trade_date=market_day,
                           plan_id=plan_id, signal=signal)
        return res

    def _submit_core(
        self,
        intent: TradeIntent,
        *,
        bar: Bar | None,
        market_day: date,
        asof: date,
        regime: RegimeSnapshot,
        instrument: InstrumentInfo | None,
        sym_industry: dict[str, str],
        daily_open_count: int = 0,
        high_corr_count: int = 0,
        plan_id: str = "plan",
        seq: int = 0,
        signal: str = "REBALANCE",
        reduce_shares: int = 0,
        exact_buy_shares: int = 0,
    ) -> tuple[ExecutionResult, dict]:
        meta = {"price": None, "volume": 0, "idempotency_key": ""}
        sym = intent.symbol
        side = intent.side
        action = intent.action

        if bar is None:
            return (ExecutionResult(False, sym, action, rejected_by="gateway", reason="无行情_bar"), meta)

        entry = intent.entry_ref_price or (bar.open if bar.open else bar.close)
        # 价空间防护：intent.entry_ref_price 可能来自复权空间（如研判链的
        # 后复权收盘），与真实 bar 价偏差过大时坚决改用真实价 —— 宁可放弃
        # 参考价，也不能把复权价当真钱报给市场。
        bar_px = float(bar.open if bar.open else (bar.close or 0))
        if bar_px > 0 and entry and abs(entry / bar_px - 1.0) > 0.15:
            logger.warning("价空间纠偏 %s: 参考价 %.4f 偏离真实价 %.4f，改用真实价",
                           sym, entry, bar_px)
            entry = bar_px
        stop_price = self._stop_price(intent, entry)
        avg_vol = float(getattr(bar, "amount", 0) or 0)
        avg_vol = max(avg_vol, 1.0)

        # ---- 仓位（仅买入需要算股数）----
        if side is Side.BUY:
            if exact_buy_shares > 0:
                # 精确回补路径（T+0 做T 买回等）：按调用方给定股数直接下单，
                # 不走共享 PositionSizer —— 做T买回必须精确匹配当日卖出量（底仓不变）。
                # 仍走 OrderGuard → Gate-1 → Gateway → 记账 全链（P3）；
                # 单笔名义金额以 Gate-1 max_order_value_ratio 为兜底上限，防失控加仓。
                shares = int(exact_buy_shares) // 100 * 100
                cap_shares = 0
                if entry > 0:
                    cap_shares = int(self.portfolio.total_asset
                                     * self.risk.max_single_cash_pct / entry)
                shares = min(shares, cap_shares) if cap_shares > 0 else shares
                if shares <= 0:
                    return (ExecutionResult(False, sym, action, rejected_by="sizer",
                                            reason="精确回补股数非法（不足一手或超单笔上限）"),
                            meta)
                meta["volume"] = shares
                exact_buy = True
            else:
                ctx = SizingContext(
                    total_asset=self.portfolio.total_asset,
                    available_cash=self.portfolio.cash,
                    entry_price=entry,
                    stop_price=stop_price,
                    avg_volume_20d=avg_vol,
                    regime=regime.regime,
                    current_weight=self.portfolio.position_weight(sym),
                    industry=sym_industry.get(sym, instrument.industry if instrument else ""),
                    portfolio_risk_remain=max(
                        0.0, self.total_risk_budget - self._used_risk()),
                )
                sizing = self.sizer.suggest(intent, ctx)
                if not sizing.ok():
                    return (ExecutionResult(False, sym, action, rejected_by="sizer",
                                            reason="; ".join(sizing.reasons)), meta)
                shares = sizing.shares
                meta["volume"] = shares
                exact_buy = False
        else:
            pos = self.portfolio.positions.get(sym)
            if pos is None:
                return (ExecutionResult(False, sym, action, rejected_by="risk", reason="无持仓可卖"), meta)
            if intent.action == "SELL":
                shares = pos.can_use
            elif reduce_shares > 0:
                # Gate-2 分批止盈给出的精确减仓量，不超过可卖数量
                shares = min(reduce_shares // 100 * 100, pos.can_use) or pos.can_use
            else:
                shares = max(100, int(pos.shares * 0.5))
            meta["volume"] = shares

        order = Order(
            order_id=f"{plan_id}_{seq}_{sym}",
            symbol=sym, side=side, quantity=shares,
            price=round(entry, 4), order_type=OrderType.LIMIT,
            idempotency_key="",
            created_at=None,
        )
        meta["price"] = order.price

        # ---- Guard ----
        gr = self.guard.allow(order, plan_id=plan_id, day=market_day, seq=seq, signal=signal)
        order.idempotency_key = gr.idempotency_key
        meta["idempotency_key"] = gr.idempotency_key or ""
        if not gr.ok:
            return (ExecutionResult(False, sym, action, rejected_by="guard", reason=gr.reason), meta)

        # ---- Risk Gate-1 ----
        market = {"price": entry,
                  "limit_up": getattr(bar, "limit_up", None),
                  "limit_down": getattr(bar, "limit_down", None)}
        verdict = self.risk.check_pre_trade(
            intent, portfolio=self.portfolio,
            regime=regime, killswitch=self.killswitch,
            instrument=instrument, market=market, sym_industry=sym_industry,
            daily_open_count=daily_open_count, high_corr_count=high_corr_count,
        )
        if not verdict.allow:
            return (ExecutionResult(False, sym, action, rejected_by="risk",
                                    reason="; ".join(verdict.violations),
                                    risk_violations=verdict.violations), meta)

        # ---- Gateway 撮合 ----
        fill = self.gateway.submit(order, market_day, bar, self.cost)
        if fill is None:
            return (ExecutionResult(False, sym, action, rejected_by="gateway",
                                    reason="当日未成交（限价未触达）"), meta)
        self.portfolio.apply_fill(fill, fill.total_fee, is_buy=(side is Side.BUY),
                                  signal=signal, asof=market_day)
        if side is Side.BUY and not exact_buy:
            # 精确回补路径不覆盖持仓风控元数据：止损/止盈/持有期属于原始开仓
            # 决策，做T买回只是执行细节，绝不能用回补 intent 冲掉主策略的风险设定。
            self._attach_risk_meta(sym, intent, stop_price, sym_industry, asof)
        self.guard.mark_submitted(order, gr.idempotency_key, signal=signal)
        self.guard.clear_pending(sym)
        return (ExecutionResult(True, sym, action, shares=shares, fill=fill), meta)

    def _record_order(self, res: ExecutionResult, meta: dict, *,
                      trade_date: date, plan_id: str, signal: str) -> None:
        """委托留痕：成交与被拒都写 orders 表（对账/审计/页面展示依赖）。
        落库失败只记日志，绝不反过来弄挂交易主流程。"""
        if self.repos is None:
            return
        try:
            if res.ok and res.fill is not None:
                status, filled, avg_px = "FILLED", res.fill.quantity, res.fill.price
                gid = res.fill.fill_id
                idem = meta["idempotency_key"] or f"fill_{plan_id}_{res.symbol}"
            else:
                status = "GUARD_BLOCKED" if res.rejected_by == "guard" else "REJECTED"
                filled, avg_px, gid = 0, None, None
                idem = (meta["idempotency_key"]
                        or f"rej_{plan_id}_{res.symbol}_{int(time.time() * 1000)}")
            # Web 手动下单固定 plan_id="webui"+seq=0，guard 键不含时间维度，
            # 同日同标的第二笔（尤其被拒单）会撞 UNIQUE 约束被静默跳过留痕，
            # 手动单每次都带毫秒时间戳，保证笔笔留痕可查。
            if plan_id == "webui":
                idem = f"{status.lower()}_webui_{res.symbol}_{int(time.time() * 1000)}"
            self.repos.orders.create(
                idempotency_key=idem, plan_id=plan_id, trade_date=trade_date,
                symbol=res.symbol, side="BUY" if res.action == "BUY" else "SELL",
                order_type="LIMIT", price=meta["price"], volume=int(meta["volume"] or 0),
                filled_volume=filled, avg_fill_price=avg_px, status=status,
                gateway_order_id=gid, signal=signal,
                reject_reason="" if res.ok else f"{res.rejected_by}: {res.reason}"[:500],
            )
        except Exception as exc:                       # noqa: BLE001
            if "UNIQUE" in str(exc) and "idempotency" in str(exc):
                # 防重键已存在 = 同一笔逻辑委托今日已留过痕，正是幂等键的职责
                logger.debug("委托留痕跳过（防重） %s", res.symbol)
            else:
                logger.warning("委托留痕失败 %s: %s", res.symbol, exc)

    # ----------------------------------------------------- 内部
    def _stop_price(self, intent: TradeIntent, entry: float) -> float:
        if intent.stop_loss_type == "FIXED_PCT":
            return entry * (1 - intent.stop_loss_value)
        if intent.stop_loss_type == "ATR":
            return entry - intent.stop_loss_value * entry * 0.1  # 简化：用 entry*0.1 当 ATR 代理
        # STRUCTURE：value>1 视为绝对价位，否则按百分比兑成 entry*0.93 兜底
        if intent.stop_loss_value > 1:
            return float(intent.stop_loss_value)
        return entry * (1 - intent.stop_loss_value) if intent.stop_loss_value > 0 else entry * 0.93

    def _used_risk(self) -> float:
        total = self.portfolio.total_asset or 1.0
        used = 0.0
        for p in self.portfolio.positions.values():
            if p.stop_loss_price:
                used += (p.avg_cost - p.stop_loss_price) * p.shares
        return max(0.0, used / total)

    def _attach_risk_meta(self, sym, intent, stop_price, sym_industry, asof) -> None:
        pos = self.portfolio.positions.get(sym)
        if pos is None:
            return
        pos.stop_loss_price = stop_price
        pos.stop_loss_type = intent.stop_loss_type
        pos.take_profit = [tp.model_dump() for tp in intent.take_profit]
        pos.invalidation_checks = list(intent.invalidation_checks)
        pos.max_holding_days = intent.max_holding_days
        pos.opened_at = asof
        pos.industry = sym_industry.get(sym, pos.industry)
        if not pos.origin_shares:          # 分批止盈按建仓原始股数算比例
            pos.origin_shares = pos.shares
