"""实盘网关（QMTGateway）冒烟测试。

没有 QMT 客户端也要能验证逻辑，所以这里用一个行为逼真的假交易端 ``FakeTrader``：
它按真实 QMT 的返回约定工作（connect 返回 0、order_stock 返回委托号、
委托状态从"已报"逐步走到"已成"），从而能把网关最容易出事的路径全部走一遍：

- 下单后必须**确认成交**才返回 Fill，不能发出去就当成交了；
- 超时未成交要**先撤单**再确认，不留悬空委托；
- 断线要**联动 KillSwitch** 并置对账标记，恢复后开仓仍被拦、平仓照常放行；
- 连接失败要按指数退避重试，最终失败也只能返回 None，不能把主流程带崩。
"""

from __future__ import annotations
import logging

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt_trade.core.config import Settings                       # noqa: E402
from qmt_trade.core.trading import Order, OrderType, Side        # noqa: E402
from qmt_trade.datahub.types import Bar                          # noqa: E402
from qmt_trade.execution.costs import CostModel                  # noqa: E402
from qmt_trade.execution.gateway.qmt import (                    # noqa: E402
    ORDER_CANCELED, ORDER_JUNK, ORDER_SUCCEEDED, QMTConnection, QMTGateway,
    normalize_symbol,
)
from qmt_trade.execution.reconcile import Reconciler             # noqa: E402
from qmt_trade.ops import MemoryChannel, Notifier                # noqa: E402
from qmt_trade.risk.killswitch import KillMode, KillSwitch       # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger(__name__)

PASS = FAIL = 0
D = date(2026, 8, 7)


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        logger.info(f"  [OK]   {name} {extra}")
    else:
        FAIL += 1
        logger.info(f"  [FAIL] {name} {extra}")
    return bool(cond)


# ------------------------------------------------------------------ 假交易端
class FakeOrder:
    def __init__(self, oid, code, volume, price, otype):
        self.order_id = oid
        self.stock_code = code
        self.order_volume = volume
        self.price = price
        self.order_type = otype
        self.traded_volume = 0
        self.traded_price = 0.0
        self.order_status = 50          # 已报


class FakePosition:
    def __init__(self, code, volume, can_use, cost):
        self.stock_code = code
        self.volume = volume
        self.can_use_volume = can_use
        self.open_price = cost
        self.market_value = volume * cost


class FakeAsset:
    def __init__(self, cash, mv):
        self.cash = cash
        self.frozen_cash = 0.0
        self.market_value = mv
        self.total_asset = cash + mv


class FakeTrade:
    def __init__(self, code, otype, volume, price):
        self.stock_code = code
        self.order_type = otype
        self.traded_volume = volume
        self.traded_price = price
        self.traded_time = int(datetime(2026, 8, 7, 9, 35).timestamp())
        self.traded_id = f"t{volume}"
        self.order_id = 1


class FakeTrader:
    """行为逼真的 QMT 替身。

    ``fill`` 决定成交行为：``full`` 全成 / ``part`` 半成 / ``none`` 不成 /
    ``junk`` 废单 / ``reject`` 下单即被拒。
    """

    def __init__(self, *, fill="full", connect_rc=0, subscribe_rc=0,
                 fail_times=0, positions=None, cash=100_000.0, trades=None):
        self.fill = fill
        self.connect_rc = connect_rc
        self.subscribe_rc = subscribe_rc
        self.fail_times = fail_times        # 前 N 次 connect 抛异常
        self.started = self.stopped = 0
        self.attempts = 0
        self.orders: dict[int, FakeOrder] = {}
        self.cancelled: list[int] = []
        self.seq = 0
        self._positions = positions or []
        self._cash = cash
        self._trades = trades or []

    # -- 连接
    def start(self):
        self.started += 1
        return None                          # QMT 真实行为：返回 None 也是成功

    def connect(self):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise ConnectionRefusedError("QMT 客户端未启动")
        return self.connect_rc

    def subscribe(self, acc):
        return self.subscribe_rc

    def stop(self):
        self.stopped += 1

    # -- 交易
    def order_stock(self, acc, code, otype, volume, price_type, price, name, remark):
        if self.fill == "reject":
            return -1
        self.seq += 1
        o = FakeOrder(self.seq, code, volume, price, otype)
        if self.fill == "full":
            o.traded_volume, o.traded_price = volume, (price if price > 0 else 10.0)
            o.order_status = ORDER_SUCCEEDED
        elif self.fill == "part":
            o.traded_volume = volume // 2
            o.traded_price = price if price > 0 else 10.0
            o.order_status = 55              # 部成，会一直挂着直到超时撤单
        elif self.fill == "junk":
            o.order_status = ORDER_JUNK
        self.orders[o.order_id] = o
        return o.order_id

    def query_stock_order(self, acc, oid):
        return self.orders.get(oid)

    def query_stock_orders(self, acc):
        return list(self.orders.values())

    def cancel_order_stock(self, acc, oid):
        self.cancelled.append(oid)
        o = self.orders.get(oid)
        if o is not None:
            o.order_status = ORDER_CANCELED  # 部成部撤后 traded_volume 保留
        return 0

    # -- 查询
    def query_stock_positions(self, acc):
        return self._positions

    def query_stock_asset(self, acc):
        return FakeAsset(self._cash, sum(p.market_value for p in self._positions))

    def query_stock_trades(self, acc):
        return self._trades


def _settings() -> Settings:
    return Settings.load()


def _conn(trader: FakeTrader, **kw) -> QMTConnection:
    c = QMTConnection(_settings(), factory=lambda: (trader, "ACC"), **kw)
    c.max_retries = 3
    c.max_backoff = 0.01                     # 测试里不要真等
    return c


def _gw(trader: FakeTrader, **kw) -> QMTGateway:
    ks = kw.pop("killswitch", None)
    notifier = kw.pop("notifier", None)
    gw = QMTGateway(_settings(), connection=_conn(trader, killswitch=ks,
                                                  notifier=notifier),
                    killswitch=ks, notifier=notifier)
    gw.poll_interval = 0.01
    gw.timeout = kw.pop("timeout", 0.05)
    return gw


def _order(side=Side.BUY, qty=200, price=10.0, otype=OrderType.LIMIT) -> Order:
    return Order(order_id="o1", symbol="600519.SH", side=side, quantity=qty,
                 price=price, order_type=otype, idempotency_key="k1")


BAR = Bar(symbol="600519.SH", date=D, open=10.0, high=10.5, low=9.8, close=10.2,
          volume=1_000_000, amount=10_200_000.0)
COST = CostModel()


# ==================================================================== 代码
def test_symbol() -> None:
    logger.info("\n[1] 代码规范化 —— 补全交易所后缀")
    cases = {"600519": "600519.SH", "000001": "000001.SZ", "300750": "300750.SZ",
             "688981": "688981.SH", "430047": "430047.BJ", "159915": "159915.SZ",
             "600519.SH": "600519.SH", "sz000001": "SZ000001"}
    for raw, want in cases.items():
        check(f"{raw} → {want}", normalize_symbol(raw) == want,
              normalize_symbol(raw))


# ==================================================================== 连接
def test_connection() -> None:
    logger.info("\n[2] 连接 —— 正常建立")
    t = FakeTrader()
    c = _conn(t)
    check("连接成功", c.connect() and c.connected)
    check("已启动交易线程", t.started == 1)
    check("首次连接不需要对账", not c.needs_reconcile)
    check("重复连接不重开", c.connect() and t.started == 1)

    logger.info("\n[3] 连接 —— 失败按指数退避重试后放弃")
    t = FakeTrader(fail_times=99)
    ks = KillSwitch()
    c = _conn(t, killswitch=ks)
    check("返回 False 而不是抛异常", c.connect() is False)
    check("重试了 max_retries 次", t.attempts == 3, str(t.attempts))
    check("错误原因可读", "ConnectionRefusedError" in c.last_error, c.last_error)
    check("失败也清理了句柄", c.trader is None)

    logger.info("\n[4] 连接 —— 前几次失败后成功")
    t = FakeTrader(fail_times=2)
    c = _conn(t)
    check("第 3 次成功", c.connect() and c.connected, str(t.attempts))

    logger.info("\n[5] 连接 —— 错误码非 0 视为失败")
    for kw in ({"connect_rc": -1}, {"subscribe_rc": -1}):
        c = _conn(FakeTrader(**kw))
        check(f"{list(kw)[0]} 非 0 → 失败", c.connect() is False)

    logger.info("\n[6] 断线 —— 拉 KillSwitch 并置对账标记")
    ks, mem = KillSwitch(), MemoryChannel()
    n = Notifier(_settings(), channels=[mem])
    c = _conn(FakeTrader(), killswitch=ks, notifier=n)
    c.connect()
    c.on_disconnected("网络中断")
    check("切 REDUCE_ONLY", ks.mode is KillMode.REDUCE_ONLY, ks.mode.value)
    check("置对账标记", c.needs_reconcile)
    check("标记为未连接", not c.connected)
    check("发出 CRITICAL 告警", mem.sent and mem.sent[-1].level.name == "CRITICAL")

    logger.info("\n[7] 重连 —— 断线后重连仍要求对账")
    t = FakeTrader()
    ks = KillSwitch()
    c = _conn(t, killswitch=ks)
    c.connect()
    c.on_disconnected("闪断")
    ks.reset("人工先恢复了")
    check("ensure 自动重连", c.ensure() and c.connected)
    check("重连后重新拉闸", ks.mode is KillMode.REDUCE_ONLY, ks.mode.value)
    check("对账标记仍在", c.needs_reconcile)
    c.mark_reconciled()
    check("对账后可清标记", not c.needs_reconcile)

    logger.info("\n[8] 断线 —— 通知通道炸了不影响状态机（P4）")

    class Boom:
        def notify(self, *a, **kw):
            raise RuntimeError("webhook 500")

    ks = KillSwitch()
    c = _conn(FakeTrader(), killswitch=ks, notifier=Boom())
    c.connect()
    try:
        c.on_disconnected("测试")
        check("状态仍正确切换", ks.mode is KillMode.REDUCE_ONLY and c.needs_reconcile)
    except Exception as exc:                    # pragma: no cover
        check("状态仍正确切换", False, repr(exc))

    logger.info("\n[9] 关闭 —— 幂等")
    t = FakeTrader()
    c = _conn(t)
    c.connect()
    c.close()
    c.close()
    check("已断开", not c.connected)
    check("stop 被调用", t.stopped >= 1)


# ==================================================================== 下单
def test_submit() -> None:
    logger.info("\n[10] 下单 —— 全部成交返回已确认 Fill")
    t = FakeTrader(fill="full")
    gw = _gw(t)
    gw.conn.connect()
    fill = gw.submit(_order(), D, BAR, COST)
    check("返回 Fill", fill is not None)
    check("数量正确", fill and fill.quantity == 200)
    check("买单让价后成交", fill and fill.price > 10.0, str(fill.price if fill else None))
    check("费用按同一套模型算", fill and fill.commission > 0 and fill.stamp_tax == 0.0)
    check("未撤单", not t.cancelled)

    logger.info("\n[11] 下单 —— 卖出计印花税、方向码正确")
    t = FakeTrader(fill="full")
    gw = _gw(t)
    gw.conn.connect()
    fill = gw.submit(_order(side=Side.SELL), D, BAR, COST)
    check("印花税只在卖出收", fill and fill.stamp_tax > 0)
    check("卖单让价更低", fill and fill.price < 10.0, str(fill.price if fill else None))
    check("方向码 24", list(t.orders.values())[0].order_type == 24)

    logger.info("\n[12] 下单 —— 委托被拒返回 None 而不是假成交")
    gw = _gw(FakeTrader(fill="reject"))
    gw.conn.connect()
    check("返回 None", gw.submit(_order(), D, BAR, COST) is None)
    check("原因可读", "委托被拒" in gw.last_error, gw.last_error)

    logger.info("\n[13] 下单 —— 废单不当成交")
    gw = _gw(FakeTrader(fill="junk"))
    gw.conn.connect()
    check("返回 None", gw.submit(_order(), D, BAR, COST) is None)
    check("原因是未成交", "未成交" in gw.last_error, gw.last_error)

    logger.info("\n[14] 下单 —— 超时未成交必须先撤单")
    t = FakeTrader(fill="none")
    gw = _gw(t)
    gw.conn.connect()
    fill = gw.submit(_order(), D, BAR, COST)
    check("不返回成交", fill is None)
    check("已撤单", len(t.cancelled) == 1, str(t.cancelled))

    logger.info("\n[15] 下单 —— 部分成交：撤掉剩余并按实际成交量记账")
    t = FakeTrader(fill="part")
    gw = _gw(t)
    gw.conn.connect()
    fill = gw.submit(_order(qty=200), D, BAR, COST)
    check("返回部分成交", fill is not None and fill.quantity == 100,
          str(fill.quantity if fill else None))
    check("剩余已撤", len(t.cancelled) == 1)

    logger.info("\n[16] 下单 —— 市价单走最新价类型")
    t = FakeTrader(fill="full")
    gw = _gw(t)
    gw.conn.connect()
    gw.submit(_order(otype=OrderType.MARKET, price=None), D, BAR, COST)
    o = list(t.orders.values())[0]
    check("价格传 -1", o.price == -1.0, str(o.price))

    logger.info("\n[17] 下单 —— 未连接直接返回 None")
    gw = _gw(FakeTrader(fail_times=99))
    check("返回 None", gw.submit(_order(), D, BAR, COST) is None)
    check("原因是未连接", "未连接" in gw.last_error, gw.last_error)

    logger.info("\n[18] 下单 —— 数量为 0 不发单")
    t = FakeTrader()
    gw = _gw(t)
    gw.conn.connect()
    check("返回 None", gw.submit(_order(qty=0), D, BAR, COST) is None)
    check("没有发出委托", not t.orders)

    logger.info("\n[19] 下单 —— 交易端抛异常时拉 KillSwitch 并返回 None（P4）")

    class ExplodingTrader(FakeTrader):
        def order_stock(self, *a, **kw):
            raise RuntimeError("交易端崩了")

    ks = KillSwitch()
    gw = _gw(ExplodingTrader(), killswitch=ks)
    gw.conn.connect()
    check("返回 None", gw.submit(_order(), D, BAR, COST) is None)
    check("拉闸", ks.mode is KillMode.REDUCE_ONLY, ks.mode.value)
    check("异常写进 last_error", "RuntimeError" in gw.last_error, gw.last_error)

    logger.info("\n[20] 下单 —— 断线未对账时禁开仓、放平仓")
    t = FakeTrader(fill="full")
    gw = _gw(t)
    gw.conn.connect()
    gw.conn.needs_reconcile = True
    check("买入被拦", gw.submit(_order(side=Side.BUY), D, BAR, COST) is None)
    check("原因是未对账", "未对账" in gw.last_error, gw.last_error)
    check("卖出照常放行", gw.submit(_order(side=Side.SELL), D, BAR, COST) is not None)

    logger.info("\n[21] 收盘清理 —— 撤掉所有未了结委托")
    t = FakeTrader(fill="none")
    gw = _gw(t)
    gw.conn.connect()
    gw.submit(_order(), D, BAR, COST)        # 这一笔超时已被撤
    t.orders[99] = FakeOrder(99, "000001.SZ", 100, 10.0, 23)  # 残留挂单
    check("清理返回撤单数", gw.cancel_all() == 1, str(t.cancelled))


# ============================================================ BrokerView
def test_broker_view() -> None:
    logger.info("\n[22] BrokerView —— 持仓归一化")
    t = FakeTrader(positions=[FakePosition("600519.SH", 200, 200, 1650.0),
                              FakePosition("000001.SZ", 0, 0, 10.0)])
    gw = _gw(t)
    gw.conn.connect()
    ps = gw.query_positions()
    check("零股被剔除", len(ps) == 1, str(ps))
    check("字段齐全", ps[0]["symbol"] == "600519.SH" and ps[0]["volume"] == 200
          and ps[0]["can_use"] == 200)

    logger.info("\n[23] BrokerView —— 资产归一化")
    gw = _gw(FakeTrader(cash=123_456.78))
    gw.conn.connect()
    a = gw.query_asset()
    check("现金正确", abs(a["cash"] - 123_456.78) < 1e-6, str(a["cash"]))
    check("含总资产字段", "total_asset" in a and "market_value" in a)

    logger.info("\n[24] BrokerView —— 成交归一化（方向码转文字）")
    gw = _gw(FakeTrader(trades=[FakeTrade("600519.SH", 23, 200, 1688.0),
                                FakeTrade("000858.SZ", 24, 500, 155.0)]))
    gw.conn.connect()
    ts = gw.query_trades(D)
    check("两笔都在", len(ts) == 2, str(len(ts)))
    check("23=BUY", ts[0]["side"] == "BUY")
    check("24=SELL", ts[1]["side"] == "SELL")

    logger.info("\n[25] BrokerView —— 非当日成交被过滤")
    gw = _gw(FakeTrader(trades=[FakeTrade("600519.SH", 23, 200, 1688.0)]))
    gw.conn.connect()
    check("换个日期查不到", gw.query_trades(date(2026, 8, 6)) == [])

    logger.info("\n[26] BrokerView —— 未连接时抛错而不是返回空（避免对账误判为空账户）")
    gw = _gw(FakeTrader(fail_times=99))
    try:
        gw.query_positions()
        check("抛出异常", False)
    except RuntimeError as exc:
        check("抛出异常", "未连接" in str(exc), str(exc))

    logger.info("\n[27] 集成 —— 网关直接喂给 Gate-3 对账")
    t = FakeTrader(positions=[FakePosition("600519.SH", 200, 200, 1650.0)],
                   cash=100_000.0)
    gw = _gw(t)
    gw.conn.connect()
    res = Reconciler(_settings()).run(D, gw, local_positions={"600519.SH": 200},
                                      local_cash=100_000.0)
    check("对得上即通过", res.passed, str([d.kind for d in res.discrepancies]))
    ks = KillSwitch()
    res = Reconciler(_settings(), killswitch=ks).run(
        D, gw, local_positions={"600519.SH": 100}, local_cash=100_000.0)
    check("对不上则阻断", not res.passed and ks.mode is KillMode.REDUCE_ONLY)


def main() -> int:
    logger.info("=" * 64)
    logger.info("QMT 实盘网关 冒烟测试")
    logger.info("=" * 64)
    test_symbol()
    test_connection()
    test_submit()
    test_broker_view()
    logger.info("\n" + "=" * 64)
    logger.info(f"结果: {PASS} 通过 / {FAIL} 失败")
    logger.info("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())