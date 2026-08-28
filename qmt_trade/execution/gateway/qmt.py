"""QMT 实盘网关（设计 6.8.1 / P7）。

与 ``SimGateway`` 实现同一个 ``Gateway`` 契约，上层 ``ExecutionService`` /
``OrderGuard`` / ``CostModel`` 的代码路径完全不变——这是 P7 的落点。

相对 ``qmt_etf`` 的既有实现，这里修正了三处会真金白银出事的地方：

1. **下单后必须确认回报。** qmt_etf 用 ``order_stock_async`` 发完就返回，
   成交与否全靠回调；一旦回调线程挂了或程序重启，本地账本就永远错了。
   这里改为同步下单 + 轮询委托状态直到终态，``submit()`` 返回的是**已确认的成交**。
2. **断线不能只打日志。** 断线期间的成交回报必然丢失，本地账本立刻失真。
   这里重连成功后强制置 ``needs_reconcile``，并把 KillSwitch 拉到 REDUCE_ONLY，
   直到对账通过（Gate-3）才允许继续开仓。
3. **挂单不许过夜。** qmt_etf 靠外部定时任务扫未成交单。这里在 ``submit()``
   内部就带超时撤单：到点未成交先撤再确认部分成交，绝不留悬空委托。

环境缺失（非 Windows / 未装 xtquant / QMT 客户端没开）时本模块**不会 import 崩**，
``is_connected()`` 返回 False，``submit()`` 返回 None 并记录原因——
这样单测和 Linux 上的回测都能正常跑。
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime
from typing import Any, Callable

from ...core.config import Secrets
from ...core.logging import get_logger
from ...core.trading import Fill, Order, OrderType, Side
from ...datahub.types import Bar
from ..costs import CostModel
from .base import Gateway

logger = get_logger("execution.gateway.qmt")

# ---------------------------------------------------------------- QMT 常量
# 刻意写死而不是 from xtquant import xtconstant：
# 缺少 xtquant 的环境也要能 import 本模块并跑单测。
STOCK_BUY = 23
STOCK_SELL = 24
FIX_PRICE = 11          # 限价
LATEST_PRICE = 5        # 最新价（近似市价）

#: 委托终态：到了这些状态就不用再轮询了
ORDER_CANCELED = 54
ORDER_PART_CANCEL = 53
ORDER_SUCCEEDED = 56
ORDER_JUNK = 57         # 废单
_FINAL_STATUS = {ORDER_PART_CANCEL, ORDER_CANCELED, ORDER_SUCCEEDED, ORDER_JUNK}
_DEAD_STATUS = {ORDER_JUNK}


def normalize_symbol(code: str) -> str:
    """补全交易所后缀。``600519`` → ``600519.SH``。"""
    code = str(code).strip().upper()
    if "." in code:
        return code
    if not code.isdigit():
        return code
    head = code[0]
    if head in "69":
        return f"{code}.SH"
    if head in "0123":
        return f"{code}.SZ"
    if head in "48":
        return f"{code}.BJ"
    return f"{code}.SH"


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """QMT 各版本字段命名不一（``volume`` / ``m_nVolume``），逐个试。"""
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] is not None:
                return obj[n]
        return default
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


# ================================================================ 连接管理
class QMTConnection:
    """QMT 交易端连接：带指数退避重连、断线标记与对账联动。

    刻意**不做单例**。qmt_etf 用单例是为了省事，代价是测试无法隔离、
    多账户无法并存。这里由应用层持有一份实例即可。
    """

    def __init__(self, settings=None, *, factory: Callable[[], tuple[Any, Any]] | None = None,
                 killswitch=None, notifier=None):
        cfg = settings.section("execution.qmt") if settings is not None else {}
        cfg = cfg if isinstance(cfg, dict) else {}
        # miniQMT 工作目录属于环境相关配置，优先环境变量（config/.env 里的
        # QMT_MINI_PATH），与账号的取法一致；只认 YAML 会在换机器/换券商目录时
        # 悄悄连到旧路径，connect 直接返回 -1 且极难排查。
        self.path = str(Secrets.get("QMT_MINI_PATH", "")
                        or cfg.get("path", "") or "")
        # 资金账号属于敏感信息，优先环境变量，绝不要求写进 YAML
        self.account_id = str(Secrets.get("QMT_ACCOUNT_ID", "")
                              or cfg.get("account_id", "") or "")
        self.account_type = str(cfg.get("account_type", "STOCK"))
        self.max_retries = int(cfg.get("connect_retries", 5))
        self.backoff = float(cfg.get("retry_backoff", 2.0))
        self.max_backoff = float(cfg.get("max_backoff", 30.0))

        self._factory = factory or self._default_factory
        self.killswitch = killswitch
        self.notifier = notifier
        self._lock = threading.RLock()
        self.trader: Any = None
        self.account: Any = None
        self.connected = False
        #: 断线过后必须对账才能恢复开仓 —— 断线期间的成交回报一定丢了
        self.needs_reconcile = False
        self.last_error = ""
        self.connect_count = 0
        self.disconnect_count = 0

    # ------------------------------------------------------------ 构造
    def _default_factory(self) -> tuple[Any, Any]:
        """真实环境下创建 XtQuantTrader。缺依赖时抛出可读的错误。"""
        try:
            from xtquant.xttrader import XtQuantTrader     # type: ignore
            from xtquant.xttype import StockAccount        # type: ignore
        except Exception as exc:                            # pragma: no cover - 环境相关
            raise RuntimeError(f"xtquant 不可用（需 Windows + QMT 客户端）: {exc}") from exc
        if not self.path:
            raise RuntimeError("未配置 execution.qmt.path")
        if not self.account_id:
            raise RuntimeError("未配置资金账号（QMT_ACCOUNT_ID）")
        session_id = int(time.time())
        return XtQuantTrader(self.path, session_id), StockAccount(self.account_id,
                                                                  self.account_type)

    # ------------------------------------------------------------ 连接
    def connect(self) -> bool:
        """建立连接。失败按指数退避重试，全部失败返回 False（不抛）。"""
        with self._lock:
            if self.connected:
                return True
            delay = 1.0
            for attempt in range(1, self.max_retries + 1):
                try:
                    self._connect_once()
                    self.connected = True
                    self.connect_count += 1
                    self.last_error = ""
                    logger.info("QMT 连接成功（第 %d 次尝试）", attempt)
                    if self.connect_count > 1:
                        # 是重连而不是首次连接 —— 中间那段时间的回报必然缺失
                        self._flag_reconnect()
                    return True
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning("QMT 连接失败（%d/%d）: %s", attempt,
                                   self.max_retries, exc)
                    self._safe_stop()
                    if attempt < self.max_retries:
                        time.sleep(min(delay, self.max_backoff))
                        delay *= self.backoff
            self.connected = False
            self._alarm("QMT 连接失败", self.last_error)
            return False

    def _connect_once(self) -> None:
        trader, account = self._factory()
        trader.start()                       # 返回 None 也算成功（QMT 行为如此）
        rc = trader.connect()
        if rc != 0:
            raise RuntimeError(f"connect 返回错误码 {rc}")
        rc = trader.subscribe(account)
        if rc != 0:
            raise RuntimeError(f"subscribe 返回错误码 {rc}")
        self.trader, self.account = trader, account

    def _safe_stop(self) -> None:
        try:
            if self.trader is not None:
                self.trader.stop()
        except Exception:
            pass
        self.trader = None

    # ------------------------------------------------------------ 断线
    def on_disconnected(self, reason: str = "") -> None:
        """QMT 回调入口。断线立刻停开仓，不等下一次下单才发现。"""
        with self._lock:
            self.connected = False
            self.disconnect_count += 1
            self.needs_reconcile = True
        logger.error("QMT 连接断开: %s", reason or "-")
        if self.killswitch is not None:
            self.killswitch.engage(f"QMT 连接断开，回报可能丢失：{reason or '未知原因'}")
        self._alarm("⚠ QMT 连接断开", reason or "已切至 REDUCE_ONLY，需对账后恢复")

    def _flag_reconnect(self) -> None:
        self.needs_reconcile = True
        if self.killswitch is not None:
            self.killswitch.engage("QMT 重连成功，断线期间回报未知，对账通过前禁止开仓")
        self._alarm("QMT 已重连", "断线期间可能有成交回报丢失，请等待盘后对账")

    def _alarm(self, title: str, body: str = "") -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.notify(title, body, level="CRITICAL", key="qmt:conn")
        except Exception as exc:                     # 通知永远是旁路（P4）
            logger.warning("连接告警发送失败: %s", exc)

    # ------------------------------------------------------------ 使用
    def ensure(self) -> bool:
        """每次调用前确认连接可用，断了就地重连。"""
        if self.connected and self.trader is not None:
            return True
        return self.connect()

    def mark_reconciled(self) -> None:
        """对账通过后由上层调用，清掉断线标记。KillSwitch 仍需人工确认。"""
        self.needs_reconcile = False

    def close(self) -> None:
        with self._lock:
            self._safe_stop()
            self.connected = False
            logger.info("QMT 连接已关闭")


# ================================================================ 网关
class QMTGateway(Gateway):
    """实盘下单网关，同时实现 ``BrokerView`` 供 Gate-3 对账使用。"""

    def __init__(self, settings=None, *, connection: QMTConnection | None = None,
                 killswitch=None, notifier=None):
        cfg = settings.section("execution") if settings is not None else {}
        cfg = cfg if isinstance(cfg, dict) else {}
        qcfg = cfg.get("qmt", {}) if isinstance(cfg.get("qmt", {}), dict) else {}
        self.slippage_tolerance = float(cfg.get("slippage_tolerance", 0.003))
        self.timeout = float(cfg.get("pending_timeout_seconds", 60))
        self.poll_interval = float(qcfg.get("poll_interval", 0.5))
        self.strategy_name = str(qcfg.get("strategy_name", "qmt_trade"))
        self.conn = connection or QMTConnection(settings, killswitch=killswitch,
                                                notifier=notifier)
        self.killswitch = killswitch
        self.last_error = ""
        self.fill_seq = 0

    # ------------------------------------------------------------ 契约
    def is_connected(self) -> bool:
        return bool(self.conn.connected and self.conn.trader is not None)

    def submit(self, order: Order, market_day: date, bar: Bar | None,
               cost: CostModel) -> Fill | None:
        """下单 → 轮询至终态 → 超时撤单 → 返回已确认成交。

        任何异常都被吞掉并转成 ``None``（上层记为 gateway 拒单），同时把
        KillSwitch 拉到 REDUCE_ONLY——绝不"猜着继续交易"（P4）。
        """
        self.last_error = ""
        try:
            return self._submit(order, market_day, bar, cost)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("下单过程异常 %s", order.symbol)
            if self.killswitch is not None:
                self.killswitch.engage(f"下单异常（{order.symbol}）：{self.last_error}")
            return None

    def _submit(self, order: Order, market_day: date, bar: Bar | None,
                cost: CostModel) -> Fill | None:
        if not self.conn.ensure():
            self.last_error = f"QMT 未连接: {self.conn.last_error}"
            return None
        if self.conn.needs_reconcile and order.side is Side.BUY:
            # 账实可能不符时只许平不许开 —— 卖出永远放行，否则风险敞口卡死
            self.last_error = "断线后尚未对账，禁止开仓"
            logger.warning("拦下开仓单 %s：%s", order.symbol, self.last_error)
            return None

        code = normalize_symbol(order.symbol)
        volume = int(order.quantity)
        if volume <= 0:
            self.last_error = "委托数量为 0"
            return None

        price_type, price = self._price_for(order, bar)
        otype = STOCK_BUY if order.side is Side.BUY else STOCK_SELL
        oid = self.conn.trader.order_stock(
            self.conn.account, code, otype, volume, price_type, price,
            self.strategy_name, order.idempotency_key or order.order_id)
        if oid is None or int(oid) < 0:
            self.last_error = f"委托被拒（order_stock 返回 {oid}）"
            logger.error("下单失败 %s: %s", code, self.last_error)
            return None

        info = self._await_final(int(oid))
        traded = int(_attr(info, "traded_volume", "m_nTradedVolume", default=0) or 0)
        if traded <= 0:
            self.last_error = f"未成交（状态 {_attr(info, 'order_status', default='?')}）"
            logger.info("委托 %s 未成交: %s", oid, self.last_error)
            return None

        avg = float(_attr(info, "traded_price", "m_dTradedPrice", default=0.0) or 0.0)
        if avg <= 0:                       # 极少数版本不回平均价，退回参考价
            avg = float(price if price and price > 0 else (bar.close if bar else 0.0))
        if avg <= 0:
            self.last_error = "成交价无法确认"
            return None

        if traded < volume:
            logger.warning("部分成交 %s: %d/%d 股", code, traded, volume)
        return self._make_fill(order, code, traded, avg, cost)

    # ------------------------------------------------------------ 定价
    def _price_for(self, order: Order, bar: Bar | None) -> tuple[int, float]:
        """限价单按容忍度让价成交概率更高；无价可依时退回最新价。"""
        if order.order_type is OrderType.MARKET or not order.price:
            return LATEST_PRICE, -1.0
        tol = self.slippage_tolerance
        px = order.price * (1 + tol) if order.side is Side.BUY else order.price * (1 - tol)
        return FIX_PRICE, round(max(px, 0.01), 2)

    # ------------------------------------------------------------ 轮询
    def _await_final(self, oid: int) -> Any:
        """轮询到终态或超时；超时先撤单，再取一次最终成交量。"""
        deadline = time.time() + self.timeout
        info: Any = None
        while time.time() < deadline:
            info = self._query_order(oid)
            status = int(_attr(info, "order_status", "m_nOrderStatus", default=0) or 0)
            if status in _FINAL_STATUS:
                return info
            time.sleep(self.poll_interval)

        logger.warning("委托 %s 超时未成交，撤单", oid)
        self._cancel(oid)
        time.sleep(min(self.poll_interval * 2, 2.0))
        return self._query_order(oid) or info

    def _query_order(self, oid: int) -> Any:
        trader = self.conn.trader
        try:
            info = trader.query_stock_order(self.conn.account, oid)
            if info is not None:
                return info
        except Exception:
            pass
        try:                                  # 老版本没有单笔查询接口
            for o in trader.query_stock_orders(self.conn.account) or []:
                if int(_attr(o, "order_id", "m_nOrderID", default=-1) or -1) == oid:
                    return o
        except Exception as exc:
            logger.warning("查询委托 %s 失败: %s", oid, exc)
        return None

    def _cancel(self, oid: int) -> bool:
        try:
            rc = self.conn.trader.cancel_order_stock(self.conn.account, oid)
            return rc == 0
        except Exception as exc:
            logger.warning("撤单 %s 失败: %s", oid, exc)
            return False

    def cancel_all(self) -> int:
        """收盘前清理所有未了结委托。返回撤单笔数。"""
        if not self.is_connected():
            return 0
        n = 0
        try:
            for o in self.conn.trader.query_stock_orders(self.conn.account) or []:
                status = int(_attr(o, "order_status", default=0) or 0)
                if status in _FINAL_STATUS:
                    continue
                if self._cancel(int(_attr(o, "order_id", default=-1) or -1)):
                    n += 1
        except Exception as exc:
            logger.warning("批量撤单失败: %s", exc)
        return n

    # ------------------------------------------------------------ 成交构造
    def _make_fill(self, order: Order, code: str, traded: int, avg: float,
                   cost: CostModel) -> Fill:
        amount = traded * avg
        self.fill_seq += 1
        return Fill(
            fill_id=f"qmt_{int(time.time() * 1000)}_{self.fill_seq}",
            order_id=order.order_id,
            symbol=code,
            side=order.side,
            quantity=traded,
            price=round(avg, 4),
            # 券商实际扣费按对账为准；这里用与回测同一套模型估算，保证 P7 口径一致
            commission=cost.commission(amount),
            stamp_tax=cost.stamp_tax(amount) if order.side is Side.SELL else 0.0,
            transfer_fee=cost.transfer(amount),
            timestamp=datetime.now(),
        )

    # ================================================== BrokerView（Gate-3）
    def query_positions(self) -> list[dict]:
        self._require()
        out: list[dict] = []
        for p in self.conn.trader.query_stock_positions(self.conn.account) or []:
            sym = normalize_symbol(str(_attr(p, "stock_code", "m_strInstrumentID",
                                             default="") or ""))
            vol = int(_attr(p, "volume", "m_nVolume", default=0) or 0)
            if not sym or vol == 0:
                continue
            out.append({
                "symbol": sym,
                "volume": vol,
                "can_use": int(_attr(p, "can_use_volume", "m_nCanUseVolume",
                                     default=0) or 0),
                "avg_cost": float(_attr(p, "open_price", "avg_price", "m_dOpenPrice",
                                        default=0.0) or 0.0),
                "market_value": float(_attr(p, "market_value", "m_dMarketValue",
                                            default=0.0) or 0.0)})
        return out

    def query_asset(self) -> dict:
        self._require()
        a = self.conn.trader.query_stock_asset(self.conn.account)
        if a is None:
            raise RuntimeError("query_stock_asset 返回空")
        return {"cash": float(_attr(a, "cash", "m_dCash", default=0.0) or 0.0),
                "frozen_cash": float(_attr(a, "frozen_cash", "m_dFrozenCash",
                                           default=0.0) or 0.0),
                "market_value": float(_attr(a, "market_value", "m_dMarketValue",
                                            default=0.0) or 0.0),
                "total_asset": float(_attr(a, "total_asset", "m_dBalance",
                                           default=0.0) or 0.0)}

    def query_trades(self, trade_date: date) -> list[dict]:
        self._require()
        out: list[dict] = []
        for t in self.conn.trader.query_stock_trades(self.conn.account) or []:
            ts = _attr(t, "traded_time", "m_nTradedTime", default=0)
            when = self._to_date(ts)
            if when is not None and when != trade_date:
                continue                     # QMT 只返回当日流水，这里再兜一层
            otype = int(_attr(t, "order_type", "m_nOffsetFlag", default=0) or 0)
            out.append({
                "symbol": normalize_symbol(str(_attr(t, "stock_code", default="") or "")),
                "side": "SELL" if otype == STOCK_SELL else "BUY",
                "volume": int(_attr(t, "traded_volume", "m_nTradedVolume",
                                    default=0) or 0),
                "price": float(_attr(t, "traded_price", "m_dTradedPrice",
                                     default=0.0) or 0.0),
                "traded_id": str(_attr(t, "traded_id", "m_strTradeID", default="") or ""),
                "order_id": str(_attr(t, "order_id", "m_nOrderID", default="") or "")})
        return out

    @staticmethod
    def _to_date(ts: Any) -> date | None:
        try:
            v = int(ts)
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        if v > 1_000_000_000:                # 秒级时间戳
            return datetime.fromtimestamp(v).date()
        return None                          # 只有 HHMMSS，无法判日期，交给上层

    def _require(self) -> None:
        if not self.conn.ensure():
            raise RuntimeError(f"QMT 未连接: {self.conn.last_error}")
