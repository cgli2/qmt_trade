"""进程内事件总线。

修正 qmt_etf 的缺陷 #3：回调直接读写 ``strategy.pending_order / position / entry_price``，
导致 Callback 与策略强耦合、多标的并发串扰。这里改为「事件发布 + 订阅」，
QMT 回调只负责把事件丢进总线，由订阅方各自处理。
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .logging import get_logger

logger = get_logger("core.events")


class EventType(str, Enum):
    # 行情
    TICK = "TICK"
    BAR = "BAR"
    # 交易
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_PARTIAL = "ORDER_PARTIAL"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    TRADE = "TRADE"
    POSITION_CHANGED = "POSITION_CHANGED"
    # 连接
    GATEWAY_CONNECTED = "GATEWAY_CONNECTED"
    GATEWAY_DISCONNECTED = "GATEWAY_DISCONNECTED"
    # 风控
    RISK_REJECTED = "RISK_REJECTED"
    RISK_ALERT = "RISK_ALERT"
    KILLSWITCH_CHANGED = "KILLSWITCH_CHANGED"
    RECONCILE_FAILED = "RECONCILE_FAILED"
    # 业务
    NEWS = "NEWS"
    CORP_ACTION = "CORP_ACTION"
    INTENT_CREATED = "INTENT_CREATED"
    PLAN_CREATED = "PLAN_CREATED"
    # 系统
    ERROR = "ERROR"
    HEARTBEAT = "HEARTBEAT"


@dataclass
class Event:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


Handler = Callable[[Event], None]


class EventBus:
    """线程安全的同步/异步混合事件总线。

    - ``publish``  同步派发：订阅者在调用线程内执行（用于必须立即生效的风控事件）
    - ``post``     异步派发：入队，由后台线程派发（用于行情等高频事件）

    单个订阅者异常不会影响其他订阅者，也不会打断发布方。
    """

    def __init__(self, *, name: str = "default"):
        self.name = name
        self._handlers: dict[EventType, list[Handler]] = defaultdict(list)
        self._global: list[Handler] = []
        self._lock = threading.RLock()
        self._queue: "queue.Queue[Event | None]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._running = False
        self.history: list[Event] = []
        self.keep_history = 0  # >0 时保留最近 N 条，便于测试与回放

    # ------------------------------------------------------------ 订阅
    def subscribe(self, event_type: EventType | None, handler: Handler) -> Handler:
        with self._lock:
            if event_type is None:
                self._global.append(handler)
            else:
                self._handlers[event_type].append(handler)
        return handler

    def unsubscribe(self, event_type: EventType | None, handler: Handler) -> None:
        with self._lock:
            target = self._global if event_type is None else self._handlers[event_type]
            if handler in target:
                target.remove(handler)

    def on(self, event_type: EventType | None):
        """装饰器写法。"""

        def deco(fn: Handler) -> Handler:
            self.subscribe(event_type, fn)
            return fn

        return deco

    # ------------------------------------------------------------ 发布
    def publish(self, event: Event) -> None:
        with self._lock:
            handlers = list(self._handlers.get(event.type, ())) + list(self._global)
            if self.keep_history:
                self.history.append(event)
                if len(self.history) > self.keep_history:
                    del self.history[: len(self.history) - self.keep_history]
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("事件处理器异常 type=%s handler=%s", event.type, handler)

    def emit(self, event_type: EventType, source: str = "", **payload) -> Event:
        ev = Event(type=event_type, payload=payload, source=source)
        self.publish(ev)
        return ev

    def post(self, event: Event) -> None:
        self._queue.put(event)

    # ------------------------------------------------------------ 后台线程
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._loop, name=f"eventbus-{self.name}", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 2.0) -> None:
        if not self._running:
            return
        self._running = False
        self._queue.put(None)
        if self._worker:
            self._worker.join(timeout=timeout)
        self._worker = None

    def drain(self, timeout: float = 1.0) -> None:
        """等待异步队列清空，测试用。"""
        deadline = time.time() + timeout
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.005)

    def _loop(self) -> None:  # pragma: no cover - 后台线程
        while self._running:
            try:
                ev = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if ev is None:
                break
            self.publish(ev)


#: 全局默认总线。模块间共享，测试中可用 ``EventBus()`` 自建隔离实例。
bus = EventBus(name="global")
