"""风控引擎（设计 6.6）：三闸门 + Kill Switch。

本文件实现 **Gate-1（Pre-Trade，交易前置闸门）** 与账户级熔断判定。
Gate-2（持仓守护）在 execution/backtest 的持仓循环里调用 ``guard_positions``；
Gate-3（盘后校验/对账）在 execution/reconcile 与 backtest 收尾调用。

所有规则可配置、可单测、可回溯；LLM 无权绕过（P3）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, NamedTuple

from ..brain.schemas import TradeIntent
from ..core.trading import Side
from ..datahub.types import InstrumentInfo
from ..features.regime import Regime, RegimeSnapshot
from ..portfolio.state import PortfolioState
from .killswitch import KillMode, KillSwitch

logger = logging.getLogger(__name__)

#: 逻辑止损可解析的失效条件：“跌破/下破 N 日均线”
#: 无法解析的检查项静默跳过（保守：宁可漏触发，不可误杀）
_MA_CHECK_RE = re.compile(
    r"(?:跌破|下破|失守).{0,4}?(\d{1,3})\s*(?:日|天)?(?:均线|日线|ma|MA)"
)


class GuardAction(NamedTuple):
    """Gate-2 判定结果。``tag`` 为机器可读的平仓原因码。"""

    symbol: str
    action: str          # SELL / REDUCE
    reason: str          # 人读描述
    tag: str = "SIGNAL"  # STOP_LOSS / TIME_STOP / TRAILING / FLATTEN / INVALIDATE / TP_PARTIAL / REGIME_CUT
    shares: int = 0      # REDUCE 时的建议减仓股数（0 = 由执行层决定）


@dataclass
class RiskVerdict:
    allow: bool
    violations: list[str] = field(default_factory=list)
    #: 触发的账户级熔断（用于联动 KillSwitch）
    kill_reason: str = ""

    def deny(self, reason: str) -> "RiskVerdict":
        self.allow = False
        self.violations.append(reason)
        return self


class RiskEngine:
    def __init__(self, settings, hub=None):
        cfg = settings.section("risk.gate1")
        self.max_single_weight = float(cfg.get("max_single_weight", 0.15))
        self.max_industry_weight = float(cfg.get("max_industry_weight", 0.30))
        self.max_positions = int(cfg.get("max_positions", 10))
        self.max_daily_open = int(cfg.get("max_new_positions_per_day", 3))
        self.max_single_cash_pct = float(cfg.get("max_order_value_ratio", 0.10))
        self.cash_buffer = float(cfg.get("cash_buffer", 0.005))
        self.day_loss_limit = float(cfg.get("max_daily_loss", 0.02))
        self.five_day_loss_limit = float(cfg.get("max_5d_drawdown", 0.05))
        self.max_drawdown_limit = float(cfg.get("max_drawdown", 0.15))
        self.est_volume_ratio = float(cfg.get("max_volume_ratio_of_adv", 0.05))
        self.min_list_days = int(cfg.get("min_list_days", 60))

        # ---- Gate-2（持仓守护）配置 ----
        g2 = settings.section("risk.gate2")
        self.trailing_activate = float(g2.get("trailing_activate_profit", 0.08))
        self.trailing_drawdown = float(g2.get("trailing_drawdown", 0.04))
        self.partial_tp_enabled = bool(g2.get("partial_tp", True))
        # hub 仅用于逻辑止损算均线；回测/单测可不传（此时跳过 INVALIDATE 判定）
        self.hub = hub

    # -------------------------------------------------- Gate-1 前置闸门
    def check_pre_trade(
        self,
        intent: TradeIntent,
        *,
        portfolio: PortfolioState,
        regime: RegimeSnapshot,
        killswitch: KillSwitch,
        instrument: InstrumentInfo | None,
        market: dict[str, float],
        sym_industry: dict[str, str],
        daily_open_count: int = 0,
        high_corr_count: int = 0,
    ) -> RiskVerdict:
        v = RiskVerdict(allow=True)
        sym = intent.symbol
        side = intent.side
        price = market.get("price", 0.0)
        limit_up = market.get("limit_up")
        limit_down = market.get("limit_down")

        # ---- Kill Switch 联动 ----
        if killswitch.mode is KillMode.FLATTEN:
            return v.deny("KillSwitch=FLATTEN，禁止一切交易")
        if killswitch.mode is KillMode.REDUCE_ONLY and side is Side.BUY:
            return v.deny("KillSwitch=REDUCE_ONLY，禁止开仓")

        # ---- 账户级熔断（先于个股）----
        if portfolio.day_loss > self.day_loss_limit * portfolio.initial_asset:
            killswitch.engage(f"当日亏损 {portfolio.day_loss:.0f} 超 {self.day_loss_limit:.0%}")
            return v.deny("当日亏损超限")
        if portfolio.five_day_loss > self.five_day_loss_limit * portfolio.initial_asset:
            killswitch.engage(f"近5日亏损 {portfolio.five_day_loss:.0f} 超 {self.five_day_loss_limit:.0%}")
            return v.deny("近5日累计回撤超限")
        if portfolio.max_drawdown > self.max_drawdown_limit:
            killswitch.flatten(f"最大回撤 {portfolio.max_drawdown:.0%} 触线")
            return v.deny("最大回撤触线，KillSwitch=FLATTEN")

        # ---- 标的合规 ----
        if instrument:
            if instrument.is_st:
                v.deny(f"ST/*ST 标的 {sym}")
            if instrument.is_suspended:
                v.deny(f"停牌标的 {sym}")
            if instrument.list_days(date.today()) < self.min_list_days:
                v.deny(f"上市不足 {self.min_list_days} 日 {sym}")

        # ---- 可交易性 ----
        if side is Side.BUY and limit_up is not None and price >= limit_up - 1e-6:
            v.deny(f"{sym} 已一字涨停，买不进")
        if side is Side.SELL and limit_down is not None and price <= limit_down + 1e-6:
            v.deny(f"{sym} 已一字跌停，卖不出")
        if limit_up is not None and limit_down is not None and price is not None:
            if price > limit_up + 1e-6 or price < limit_down - 1e-6:
                v.deny(f"{sym} 委托价超出涨跌停区间")

        # ---- 仓位约束 ----
        if side is Side.BUY:
            if len(portfolio.positions) >= self.max_positions:
                v.deny(f"持仓数已达上限 {self.max_positions}")
            if daily_open_count >= self.max_daily_open:
                v.deny(f"当日新开仓数已达上限 {self.max_daily_open}")
            # 单票权重（含本次估算）
            est_notional = price * 0  # 具体股数由 sizer 决定，这里只做上限预警占位
            _ = est_notional
            if intent.max_weight_hint > 0 and intent.max_weight_hint < self.max_single_weight:
                pass  # 实际权重在 ExecutionService 用 sizer 结果复核
            ind = sym_industry.get(sym, instrument.industry if instrument else "")
            if ind and portfolio.industry_weight(ind, sym_industry) >= self.max_industry_weight:
                v.deny(f"行业 {ind} 权重已达上限 {self.max_industry_weight:.0%}")
            if high_corr_count >= 3:
                v.deny(f"与现有持仓高相关(>0.8)标的数 {high_corr_count} ≥ 3")
            # 总仓位 vs Regime 上限
            if regime.max_position > 0:
                used = sum(p.shares * p.avg_cost for p in portfolio.positions.values())
                if used >= regime.max_position * portfolio.total_asset:
                    v.deny(f"总仓位已达 Regime 上限 {regime.max_position:.0%}")

        # ---- 资金 ----
        if side is Side.BUY:
            single_cap = portfolio.total_asset * self.max_single_cash_pct
            # 单笔金额上限在 ExecutionService 用 sizer 结果复核，这里只做兜底
            _ = single_cap

        # ---- 卖出可卖数量 ----
        if side is Side.SELL:
            pos = portfolio.positions.get(sym)
            if pos is None:
                v.deny(f"无 {sym} 持仓可卖")
            elif pos.can_use <= 0:
                v.deny(f"{sym} 无可卖数量（T+1）")

        return v

    # -------------------------------------------------- Gate-2 持仓守护
    def guard_positions(
        self,
        portfolio: PortfolioState,
        *,
        last_prices: dict[str, float],
        asof: date,
        killswitch: KillSwitch,
        regime: RegimeSnapshot | None = None,
        external_stops: dict[str, float] | None = None,
    ) -> list[GuardAction]:
        """返回需要执行的动作列表。

        ``action`` ∈ {SELL, REDUCE}；``tag`` 是**机器可读的平仓原因**
        （STOP_LOSS / INVALIDATE / TIME_STOP / TP_PARTIAL / TRAILING / FLATTEN），
        会一路带到成交记录里，供 L5 复盘按原因归因
        （"止损占比过高 → 止损太紧"这类结论全靠它）。

        ``external_stops``：外部策略（策略实验室独立策略）持仓的 {symbol: 绝对止损价}。
        这些持仓**只用自身止损**，跳过主因子体系的 trailing/时间止损/止盈/失效/Regime 规则
        —— 它们的离场逻辑由策略自身定义（回测同口径），主系统规则不应叠加干扰
        （2026-08-16 修复：此前 Gate-2 的 +2%/-5% 移动止盈会对 lab 持仓生效，与回测不一致）。
        """
        actions: list[GuardAction] = []
        if killswitch.mode is KillMode.FLATTEN:
            for s in list(portfolio.positions):
                actions.append(GuardAction(s, "SELL", "KillSwitch=FLATTEN 全部平仓", "FLATTEN"))
            return actions

        external_stops = external_stops or {}

        for sym, pos in list(portfolio.positions.items()):
            lp = last_prices.get(sym)
            if lp is None:
                continue
            # 价空间体检：现价与成本偏离超 50% 必然是数据异常（复权/真实价
            # 混用、陈旧兜底价等）。涨跌停也才 ±20%，此时触发止损止盈必然
            # 是假信号 —— 宁可本轮跳过并告警，也不能拿脏价误卖持仓。
            if pos.avg_cost > 0 and abs(lp / pos.avg_cost - 1.0) > 0.5:
                logger.warning("%s 价空间异常守护：现价 %.2f 偏离成本 %.2f 超 50%%，跳过本轮守护",
                               sym, lp, pos.avg_cost)
                continue
            # T+1 当日买入不可卖：此时发出任何卖出指令都必然被拒，
            # 跳过本轮（条件仍在，明日 shares 解冻后第一轮就会触发）
            if getattr(pos, "can_use", pos.shares) <= 0:
                continue
            # 外部策略持仓（策略实验室）：只用自身止损，跳过主系统离场规则
            if sym in external_stops:
                ext = float(external_stops[sym])
                if ext > 0 and lp <= ext:
                    actions.append(GuardAction(
                        sym, "SELL", f"策略止损位 {ext:.2f}", "STOP_LOSS"))
                continue
            # 固定/百分比止损
            if pos.stop_loss_price and lp <= pos.stop_loss_price:
                actions.append(GuardAction(
                    sym, "SELL", f"跌破止损位 {pos.stop_loss_price:.2f}", "STOP_LOSS"))
                continue
            # 逻辑止损：Intent 带来的失效条件（如"跌破 20 日均线"）
            act = self._check_invalidation(sym, pos, lp, asof)
            if act is not None:
                actions.append(act)
                continue
            # 时间止损
            if pos.opened_at and pos.max_holding_days:
                held = (asof - pos.opened_at).days
                if held >= pos.max_holding_days:
                    # P0 优化（2026-08-13）：已激活移动止盈的盈利仓不时间止损，
                    # 交给下方 trailing 回撤判定接管——避免 +3%~+4% 的赢家被时间止损
                    # 提前砍掉（原逻辑对输赢仓无差别时间止损 → 小赢家没机会跑、组合
                    # 靠单只 homerun 撑业绩）。峰值涨幅未达 trailing 激活线的横盘/微亏
                    # 仓仍照常时间止损，释放资金。
                    peak = (pos.highest_since_open / pos.avg_cost - 1.0) if pos.avg_cost > 0 else 0.0
                    if peak >= self.trailing_activate:
                        pass  # 已展现真实涨幅 → 不时间止损，继续走下方 trailing 回撤判定
                    else:
                        actions.append(GuardAction(
                            sym, "SELL", f"持有 {held} 日超 {pos.max_holding_days} 日上限", "TIME_STOP"))
                        continue
            # 分批止盈：到达 Intent 约定的档位 → 按比例减仓
            tp_act = self._check_take_profit(sym, pos, lp)
            if tp_act is not None:
                actions.append(tp_act)
                continue
            # 移动止盈：最高点盈利超激活线后，现价从最高点回撤超阈值
            # （激活看最高点是刻意的：避免盘中没刷到 highest 就永不激活）
            if pos.highest_since_open > 0 and pos.avg_cost > 0:
                peak_profit = pos.highest_since_open / pos.avg_cost - 1.0
                if peak_profit >= self.trailing_activate:
                    if lp <= pos.highest_since_open * (1 - self.trailing_drawdown):
                        actions.append(GuardAction(
                            sym, "SELL", "移动止盈回撤触发", "TRAILING"))
                        continue

        # ---- 总仓位管理：Regime 恶化 → 主动把总仓位压回上限 ----
        # Gate-1 只能拦住新开仓，已持仓必须在守护层降，否则 RISK_OFF 也满仓
        if regime is not None:
            decided = {a.symbol for a in actions}
            actions.extend(self._regime_deleverage(
                portfolio, last_prices, regime, skip=decided))
        return actions

    def _regime_deleverage(
        self,
        portfolio: PortfolioState,
        last_prices: dict[str, float],
        regime: RegimeSnapshot,
        *,
        skip: set[str] | None = None,
    ) -> list[GuardAction]:
        """按当前市值算总仓位，超 Regime 上限则每只等比例减仓。
        上限为 0（RISK_OFF）→ 全部清仓；本轮已被个股规则判定的标的跳过。"""
        out: list[GuardAction] = []
        skip = skip or set()
        cap = float(getattr(regime, "max_position", 1.0) or 0)
        total = portfolio.total_asset or 0.0
        if total <= 0 or not portfolio.positions:
            return out
        mv = sum(
            p.shares * float(last_prices.get(s) or p.avg_cost)
            for s, p in portfolio.positions.items())
        if mv <= cap * total * 1.02:      # 2% 缓冲，避免贴着上限反复减
            return out
        if cap <= 0:
            return [GuardAction(s, "SELL", f"Regime={regime.regime.value} 总仓位清零",
                                "REGIME_CUT") for s in portfolio.positions if s not in skip]
        # 等比例降到上限：每只减仓量 = 股数 * (1 - 目标市值/当前市值)
        shrink = max(0.0, 1.0 - cap * total / mv)
        for s, p in portfolio.positions.items():
            if s in skip:
                continue
            lp = last_prices.get(s)
            if lp is None:
                continue
            cut = int(p.shares * shrink) // 100 * 100
            if cut <= 0:
                continue
            if p.shares - cut < 200:
                cut = p.shares
            out.append(GuardAction(
                s, "SELL" if cut >= p.shares else "REDUCE",
                f"Regime={regime.regime.value} 降总仓位至 {cap:.0%}", "REGIME_CUT", cut))
        return out

    # -------------------------------------------------- Gate-2 子规则
    def _check_invalidation(self, sym: str, pos, lp: float, asof: date) -> GuardAction | None:
        """解析 invalidation_checks 中的均线失效条件；解析不了的一律跳过。"""
        if not pos.invalidation_checks or self.hub is None:
            return None
        for chk in pos.invalidation_checks:
            m = _MA_CHECK_RE.search(str(chk))
            if not m:
                continue
            n = int(m.group(1))
            ma = self._ma(sym, n, asof)
            if ma is None:
                continue
            if lp < ma:
                return GuardAction(
                    sym, "SELL", f"逻辑止损：跌破 {n} 日均线 {ma:.2f}（{chk}）", "INVALIDATE")
        return None

    def _ma(self, sym: str, n: int, asof: date) -> float | None:
        """前 n 日收盘均线（含当日 bar）。取数失败返回 None —— 保守不触发。"""
        try:
            df = self.hub.get_bars([sym], start=asof - timedelta(days=n * 3), end=asof)
        except Exception:                              # noqa: BLE001
            return None
        if df is None or len(df) < n:
            return None
        closes = df["close"].astype(float).tail(n)
        return float(closes.mean()) if len(closes) == n else None

    def _check_take_profit(self, sym: str, pos, lp: float) -> GuardAction | None:
        """分批止盈。按**建仓原始股数**乘档位比例算减仓量（整百），
        剩余不足 200 股或已是最后一档 → 直接全平。
        触发后同步记录 tp_done_levels（内存态，执行成功后由 persist 落库）。"""
        if not self.partial_tp_enabled or not pos.take_profit or pos.avg_cost <= 0:
            return None
        base = pos.origin_shares or pos.shares
        done = set(pos.tp_done_levels or [])
        levels = list(enumerate(pos.take_profit))
        for i, tp in levels:
            if i in done:
                continue
            try:
                px = float(tp.get("price_or_pct") or 0)
                ratio = float(tp.get("ratio") or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0 or ratio <= 0:
                continue
            trigger = pos.avg_cost * (1 + px) if str(tp.get("kind", "PCT")) == "PCT" else px
            if lp < trigger:
                break                                  # 档位按升序约定，未达即停
            pos.tp_done_levels = sorted(done | {i})
            cut = int(base * ratio) // 100 * 100
            last_level = i == len(levels) - 1
            if last_level or pos.shares - cut < 200:
                return GuardAction(sym, "SELL", f"止盈档位{i + 1}达成，清仓", "TP_PARTIAL")
            return GuardAction(
                sym, "REDUCE", f"止盈档位{i + 1}达成，减仓 {ratio:.0%}", "TP_PARTIAL", cut)
        return None
