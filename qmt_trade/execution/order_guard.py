"""OrderGuard（设计 6.8.3）：下单前的三道防线 + 幂等。

- 幂等键：``hash(plan_id + symbol + side + date + seq)``，进程崩溃重启/回调重复都只成一笔。
- 同标的冷却：止损类信号不冷却，其余按配置冷却。
- 单标的日内下单次数上限、全局每秒速率上限。
- 挂单未决拦截：按 symbol 维护 pending 集合，超时可配。

M1 用内存状态；生产环境幂等键应落库唯一索引（防重启后重复）。这里先记账。
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..core.trading import Order, Side


@dataclass
class GuardResult:
    ok: bool
    reason: str = ""
    idempotency_key: str = ""


class OrderGuard:
    def __init__(self, settings):
        cfg = settings.section("execution.guard")
        self.cooldown_sec = float(cfg.get("cooldown_seconds", 120))
        self.per_symbol_daily = int(cfg.get("max_orders_per_symbol_per_day", 6))
        self.global_per_sec = int(cfg.get("max_orders_per_second", 3))
        self.pending_timeout = float(cfg.get("pending_timeout", 60))
        self.stop_signals_no_cooldown = bool(cfg.get("stop_signals_no_cooldown", True))

        self._idempotent: set[str] = set()
        self._last_order_ts: dict[str, float] = {}     # symbol -> ts（冷却）
        self._symbol_daily: dict[str, int] = defaultdict(int)
        self._recent_ts: list[float] = []              # 全局速率窗口
        self._pending: dict[str, float] = {}           # symbol -> 提交时间

    @staticmethod
    def make_key(plan_id: str, symbol: str, side: Side, day: date, seq: int) -> str:
        raw = f"{plan_id}|{symbol}|{side.value}|{day.isoformat()}|{seq}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def allow(self, order: Order, *, plan_id: str, day: date, seq: int,
              signal: str = "REBALANCE") -> GuardResult:
        key = self.make_key(plan_id, order.symbol, order.side, day, seq)
        if key in self._idempotent:
            return GuardResult(False, "幂等键已存在（重复订单）", key)

        now = time.time()
        # 全局速率
        self._recent_ts = [t for t in self._recent_ts if now - t < 1.0]
        if len(self._recent_ts) >= self.global_per_sec:
            return GuardResult(False, "全局下单速率超限", key)

        # 单标的日内次数
        if self._symbol_daily[order.symbol] >= self.per_symbol_daily:
            return GuardResult(False, f"标的 {order.symbol} 日内下单超限", key)

        # 冷却（止损类除外）
        if not (self.stop_signals_no_cooldown and signal in ("STOP_LOSS", "STOP_PROFIT", "KILL")):
            last = self._last_order_ts.get(order.symbol)
            if last and (now - last) < self.cooldown_sec:
                return GuardResult(False, f"{order.symbol} 冷却中", key)

        # pending 拦截
        pend = self._pending.get(order.symbol)
        if pend and (now - pend) < self.pending_timeout:
            return GuardResult(False, f"{order.symbol} 有未决订单", key)

        return GuardResult(True, "", key)

    def mark_submitted(self, order: Order, key: str, *, signal: str = "REBALANCE") -> None:
        self._idempotent.add(key)
        now = time.time()
        self._last_order_ts[order.symbol] = now
        self._symbol_daily[order.symbol] += 1
        self._recent_ts.append(now)
        if order.is_active:
            self._pending[order.symbol] = now

    def clear_pending(self, symbol: str) -> None:
        self._pending.pop(symbol, None)

    def on_new_day(self) -> None:
        self._symbol_daily.clear()
        self._pending.clear()
