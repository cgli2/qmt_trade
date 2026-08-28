"""执行层 Gate-3 盘后对账冒烟测试。

对账是全系统唯一能发现「我以为的」和「实际的」不一致的机制，
所以这里测的重点不是"能算差异"，而是几条硬纪律：

- 默认结论是**不通过**：查询抛异常、券商不返回字段，一律判失败（P4 失败安全）；
- 发现问题必须**联动阻断**（KillSwitch → REDUCE_ONLY），而不是只打条日志
  —— 这正是修正 qmt_etf「对账只打日志」的关键点；
- 恢复必须**人工显式确认**，不能因为第二天对上了就自动放行；
- 券商说没成交、本地记了单，同样要报警（不能因 remote 为空就跳过）。
"""

from __future__ import annotations
import logging

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt_trade.core.config import Settings                       # noqa: E402
from qmt_trade.execution.reconcile import (                      # noqa: E402
    Discrepancy, Reconciler, ReconcileResult,
)
from qmt_trade.ops import Level, MemoryChannel, Notifier         # noqa: E402
from qmt_trade.risk.killswitch import KillMode, KillSwitch       # noqa: E402
from qmt_trade.storage.db import Database                        # noqa: E402
from qmt_trade.storage.models import Repos                       # noqa: E402

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


def kinds(res: ReconcileResult) -> list[str]:
    return [d.kind for d in res.discrepancies]


# ------------------------------------------------------------------ 假券商
class FakeBroker:
    """券商只读视图的替身。``boom`` 用于模拟查询超时/断线。"""

    def __init__(self, positions=None, cash=None, trades=None, *, boom: str = ""):
        self._positions = positions or []
        self._cash = cash
        self._trades = trades or []
        self.boom = boom
        self.calls: list[str] = []

    def query_positions(self):
        self.calls.append("positions")
        if self.boom == "positions":
            raise TimeoutError("连接券商超时")
        return self._positions

    def query_asset(self):
        self.calls.append("asset")
        if self.boom == "asset":
            raise ConnectionError("行情端口未就绪")
        return {} if self._cash is None else {"cash": self._cash}

    def query_trades(self, trade_date):
        self.calls.append("trades")
        if self.boom == "trades":
            raise RuntimeError("成交查询失败")
        return self._trades


def _settings() -> Settings:
    return Settings.load()


def _rec(**kw) -> Reconciler:
    return Reconciler(_settings(), **kw)


# ==================================================================== 持仓
def test_positions() -> None:
    st = _settings()

    logger.info("\n[1] 持仓 —— 完全一致即通过")
    broker = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 200},
                                   {"symbol": "000858.SZ", "volume": 500}],
                        cash=100_000.0)
    res = _rec().run(D, broker, local_positions={"600519.SH": 200, "000858.SZ": 500},
                     local_cash=100_000.0)
    check("对账通过", res.passed, f"差异={kinds(res)}")
    check("核对范围记录", res.checked.get("positions") == 2 and res.checked.get("cash") == 1,
          str(res.checked))
    check("渲染含通过字样", "通过" in res.render() and "无差异" in res.render())

    logger.info("\n[2] 持仓 —— 券商有本地无（最危险：未知成交）")
    broker = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 200}], cash=1.0)
    res = _rec().run(D, broker, local_positions={}, local_cash=1.0)
    d = res.discrepancies[0]
    check("识别 POSITION_MISSING", kinds(res) == ["POSITION_MISSING"], str(kinds(res)))
    check("严重度 CRITICAL", d.severity == "CRITICAL")
    check("判定不通过", not res.passed)
    check("本地/券商值正确", d.local == 0 and d.broker == 200)

    logger.info("\n[3] 持仓 —— 本地有券商无（幻觉持仓，卖出会失败）")
    broker = FakeBroker(positions=[], cash=1.0)
    res = _rec().run(D, broker, local_positions={"300750.SZ": 100}, local_cash=1.0)
    check("识别 POSITION_EXTRA", kinds(res) == ["POSITION_EXTRA"], str(kinds(res)))
    check("判定不通过", not res.passed)

    logger.info("\n[4] 持仓 —— 数量不符（部分成交回报丢失）")
    broker = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 300}], cash=1.0)
    res = _rec().run(D, broker, local_positions={"600519.SH": 200}, local_cash=1.0)
    check("识别 POSITION_QTY", kinds(res) == ["POSITION_QTY"], str(kinds(res)))
    check("差额写进 message", "-100" in res.discrepancies[0].message,
          res.discrepancies[0].message)
    check("判定不通过", not res.passed)

    logger.info("\n[5] 持仓 —— 零股不参与比对")
    broker = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 200},
                                   {"symbol": "000001.SZ", "volume": 0}], cash=1.0)
    res = _rec().run(D, broker, local_positions={"600519.SH": 200, "600036.SH": 0},
                     local_cash=1.0)
    check("两边的 0 都被忽略", res.passed and res.checked["positions"] == 1,
          f"{kinds(res)} {res.checked}")

    logger.info("\n[6] 持仓 —— 容忍度可配（position_tolerance）")
    r = Reconciler(st)
    r.position_tolerance = 100
    broker = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 300}], cash=1.0)
    res = r.run(D, broker, local_positions={"600519.SH": 200}, local_cash=1.0)
    check("容忍内不报", res.passed, str(kinds(res)))

    logger.info("\n[7] 持仓 —— 兼容 QMT 原生字段命名")
    broker = FakeBroker(positions=[{"stock_code": "600519.SH", "m_nVolume": 200}],
                        cash=1.0)
    res = _rec().run(D, broker, local_positions={"600519.SH": 200}, local_cash=1.0)
    check("stock_code/m_nVolume 可解析", res.passed, str(kinds(res)))

    logger.info("\n[8] 持仓 —— 同标的多行自动合并（QMT 分账户返回）")
    broker = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 100},
                                   {"symbol": "600519.SH", "volume": 100}], cash=1.0)
    res = _rec().run(D, broker, local_positions={"600519.SH": 200}, local_cash=1.0)
    check("100+100 合并为 200", res.passed, str(kinds(res)))


# ==================================================================== 现金
def test_cash() -> None:
    logger.info("\n[9] 现金 —— 容忍度内不报")
    broker = FakeBroker(positions=[], cash=100_000.50)
    res = _rec().run(D, broker, local_positions={}, local_cash=100_000.0)
    check("0.5 元差不报", res.passed, str(kinds(res)))

    logger.info("\n[10] 现金 —— 超容忍度报 ERROR")
    broker = FakeBroker(positions=[], cash=98_000.0)
    res = _rec().run(D, broker, local_positions={}, local_cash=100_000.0)
    check("识别 CASH_MISMATCH", kinds(res) == ["CASH_MISMATCH"], str(kinds(res)))
    check("判定不通过", not res.passed)
    check("差额带符号", "+2,000.00" in res.discrepancies[0].message,
          res.discrepancies[0].message)

    logger.info("\n[11] 现金 —— 券商不返回现金字段即判失败")
    broker = FakeBroker(positions=[], cash=None)
    res = _rec().run(D, broker, local_positions={}, local_cash=100_000.0)
    check("识别 CASH_UNKNOWN", kinds(res) == ["CASH_UNKNOWN"], str(kinds(res)))
    check("判定不通过（不猜）", not res.passed)

    logger.info("\n[12] 现金 —— 本地无快照只告警不阻断")
    broker = FakeBroker(positions=[], cash=100_000.0)
    res = _rec().run(D, broker, local_positions={}, local_cash=None)
    check("降级为 WARN", res.discrepancies[0].severity == "WARN")
    check("不阻断", res.passed, str(kinds(res)))


# ==================================================================== 成交
def _repos_with_trades(rows: list[dict]) -> Repos:
    repos = Repos.create(Database(":memory:"))
    for r in rows:
        repos.trades.add(trade_date=D, symbol=r["symbol"], side=r["side"],
                         price=r["price"], volume=r["volume"],
                         amount=r["price"] * r["volume"])
    return repos


def test_trades() -> None:
    logger.info("\n[13] 成交 —— 逐笔对上")
    repos = _repos_with_trades([
        {"symbol": "600519.SH", "side": "BUY", "price": 1688.0, "volume": 200},
        {"symbol": "000858.SZ", "side": "SELL", "price": 155.0, "volume": 500}])
    broker = FakeBroker(cash=1.0, trades=[
        {"symbol": "600519.SH", "side": "BUY", "price": 1688.0, "volume": 200},
        {"symbol": "000858.SZ", "side": "SELL", "price": 155.0, "volume": 500}])
    res = _rec(repos=repos).run(D, broker, local_positions={}, local_cash=1.0,
                                persist=False)
    check("全部匹配", res.passed, str(kinds(res)))
    check("成交数记录", res.checked.get("trades") == 2, str(res.checked))

    logger.info("\n[14] 成交 —— 券商有本地没有（回报丢失）")
    repos = _repos_with_trades([])
    broker = FakeBroker(cash=1.0, trades=[
        {"symbol": "600519.SH", "side": "BUY", "price": 1688.0, "volume": 200}])
    res = _rec(repos=repos).run(D, broker, local_positions={}, local_cash=1.0,
                                persist=False)
    check("识别 TRADE_UNKNOWN", kinds(res) == ["TRADE_UNKNOWN"], str(kinds(res)))
    check("CRITICAL 且不通过",
          res.discrepancies[0].severity == "CRITICAL" and not res.passed)

    logger.info("\n[15] 成交 —— 本地有券商没有（幻觉成交）")
    repos = _repos_with_trades([
        {"symbol": "600519.SH", "side": "BUY", "price": 1688.0, "volume": 200}])
    broker = FakeBroker(cash=1.0, trades=[])
    res = _rec(repos=repos).run(D, broker, local_positions={}, local_cash=1.0,
                                persist=False)
    check("券商空流水仍报警", kinds(res) == ["TRADE_PHANTOM"], str(kinds(res)))
    check("判定不通过", not res.passed)

    logger.info("\n[16] 成交 —— 滑点异常只告警")
    repos = _repos_with_trades([
        {"symbol": "600519.SH", "side": "BUY", "price": 1688.0, "volume": 200}])
    broker = FakeBroker(cash=1.0, trades=[
        {"symbol": "600519.SH", "side": "BUY", "price": 1750.0, "volume": 200}])
    res = _rec(repos=repos).run(D, broker, local_positions={}, local_cash=1.0,
                                persist=False)
    check("识别 SLIPPAGE", kinds(res) == ["SLIPPAGE"], str(kinds(res)))
    check("WARN 不阻断", res.passed and res.discrepancies[0].severity == "WARN")
    check("偏离百分比正确", "3.67%" in res.discrepancies[0].message,
          res.discrepancies[0].message)

    logger.info("\n[17] 成交 —— 同标的多笔按配对逐笔比价，不会张冠李戴")
    repos = _repos_with_trades([
        {"symbol": "600519.SH", "side": "BUY", "price": 1688.0, "volume": 200},
        {"symbol": "600519.SH", "side": "BUY", "price": 1700.0, "volume": 300}])
    broker = FakeBroker(cash=1.0, trades=[
        {"symbol": "600519.SH", "side": "BUY", "price": 1688.0, "volume": 200},
        {"symbol": "600519.SH", "side": "BUY", "price": 1700.0, "volume": 300}])
    res = _rec(repos=repos).run(D, broker, local_positions={}, local_cash=1.0,
                                persist=False)
    check("各自匹配无误报", res.passed, str(kinds(res)))

    logger.info("\n[18] 成交 —— 兼容 QMT 数字方向码与原生字段")
    repos = _repos_with_trades([
        {"symbol": "600519.SH", "side": "BUY", "price": 1688.0, "volume": 200}])
    broker = FakeBroker(cash=1.0, trades=[
        {"stock_code": "600519.SH", "m_nOffsetFlag": 48,
         "m_dTradedPrice": 1688.0, "m_nTradedVolume": 200}])
    res = _rec(repos=repos).run(D, broker, local_positions={}, local_cash=1.0,
                                persist=False)
    check("48=买入 可匹配", res.passed, str(kinds(res)))

    logger.info("\n[19] 成交 —— 方向不符视为两笔差异")
    repos = _repos_with_trades([
        {"symbol": "600519.SH", "side": "BUY", "price": 1688.0, "volume": 200}])
    broker = FakeBroker(cash=1.0, trades=[
        {"symbol": "600519.SH", "side": "SELL", "price": 1688.0, "volume": 200}])
    res = _rec(repos=repos).run(D, broker, local_positions={}, local_cash=1.0,
                                persist=False)
    check("同时报未知与幻觉",
          sorted(kinds(res)) == ["TRADE_PHANTOM", "TRADE_UNKNOWN"], str(kinds(res)))


# ============================================================== 失败安全 P4
def test_fail_safe() -> None:
    logger.info("\n[20] 失败安全 —— 券商查询异常一律判不通过")
    for stage in ("positions", "asset", "trades"):
        broker = FakeBroker(cash=1.0, boom=stage)
        res = _rec().run(D, broker, local_positions={}, local_cash=1.0)
        check(f"{stage} 异常 → 不通过", not res.passed and bool(res.error),
              res.error)

    logger.info("\n[21] 失败安全 —— 异常类型写进 error 字段便于排障")
    broker = FakeBroker(cash=1.0, boom="positions")
    res = _rec().run(D, broker, local_positions={}, local_cash=1.0)
    check("含异常类名", res.error.startswith("TimeoutError"), res.error)
    check("渲染里能看到", "执行异常" in res.render())

    logger.info("\n[22] 失败安全 —— 空账户也算通过（不是异常）")
    res = _rec().run(D, FakeBroker(positions=[], cash=0.0), local_positions={},
                     local_cash=0.0)
    check("空对空通过", res.passed, str(kinds(res)))


# ============================================================== 联动与落库
def test_integration() -> None:
    logger.info("\n[23] 联动 —— 对账不通过必须拉 KillSwitch")
    ks = KillSwitch()
    broker = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 200}], cash=1.0)
    res = _rec(killswitch=ks).run(D, broker, local_positions={}, local_cash=1.0)
    check("切到 REDUCE_ONLY", ks.mode is KillMode.REDUCE_ONLY, ks.mode.value)
    check("原因含对账", "对账未通过" in ks.reason, ks.reason)
    check("禁止开仓", not ks.allow_open)
    check("仍允许平仓", ks.allow_close)

    logger.info("\n[24] 联动 —— 对账通过不会误动 KillSwitch")
    ks = KillSwitch()
    res = _rec(killswitch=ks).run(D, FakeBroker(positions=[], cash=1.0),
                                  local_positions={}, local_cash=1.0)
    check("保持 NORMAL", res.passed and ks.mode is KillMode.NORMAL)

    logger.info("\n[25] 联动 —— 通过不自动解除既有限制（必须人工确认）")
    ks = KillSwitch()
    ks.engage("昨日对账未通过")
    _rec(killswitch=ks).run(D, FakeBroker(positions=[], cash=1.0),
                            local_positions={}, local_cash=1.0)
    check("仍是 REDUCE_ONLY", ks.mode is KillMode.REDUCE_ONLY, ks.mode.value)

    logger.info("\n[26] 通知 —— 不通过发 CRITICAL，通过只发 DEBUG")
    mem = MemoryChannel()
    n = Notifier(_settings(), channels=[mem])
    n.min_level = Level.DEBUG          # 让 DEBUG 级的"对账通过"也能被观测到
    n.throttle_seconds = 0             # 同 key 连发两次，测试里不要被节流
    broker = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 200}], cash=1.0)
    _rec(notifier=n).run(D, broker, local_positions={}, local_cash=1.0)
    check("发出 CRITICAL", mem.sent and mem.sent[-1].level.name == "CRITICAL",
          mem.sent[-1].level.name if mem.sent else "无")
    check("正文含差异明细", "POSITION_MISSING" in mem.sent[-1].body)
    before = len(mem.sent)
    _rec(notifier=n).run(D, FakeBroker(positions=[], cash=1.0),
                         local_positions={}, local_cash=1.0)
    check("通过是低噪声级别",
          len(mem.sent) == before + 1 and mem.sent[-1].level.name == "DEBUG",
          mem.sent[-1].level.name)

    logger.info("\n[27] 通知 —— 通道炸了不影响对账结论（P4）")

    class Boom:
        def notify(self, *a, **kw):
            raise RuntimeError("webhook 500")

    try:
        res = _rec(notifier=Boom()).run(D, FakeBroker(positions=[], cash=1.0),
                                        local_positions={}, local_cash=1.0)
        check("主流程未被带崩", res.passed)
    except Exception as exc:                       # pragma: no cover
        check("主流程未被带崩", False, repr(exc))

    logger.info("\n[28] 落库 —— reconcile_logs / system_state / risk_events 三处留痕")
    repos = Repos.create(Database(":memory:"))
    broker = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 200}], cash=1.0)
    res = _rec(repos=repos).run(D, broker, local_positions={}, local_cash=1.0)
    row = repos.db.query_one("SELECT * FROM reconcile_logs ORDER BY created_at DESC")
    check("写入对账日志", row is not None and int(row["passed"]) == 0)
    check("日志含差异明细 JSON", "POSITION_MISSING" in str(row["detail"]))
    check("system_state 标记", repos.system.get("reconcile_ok") == "0")
    events = repos.risk_events.list_by_date(D)
    check("风控事件落库", len(events) == 1 and events[0]["gate"] == "GATE3",
          str([e["rule"] for e in events]))

    logger.info("\n[29] 落库 —— 写库失败不改变结论，读库失败必须判不通过")
    repos = Repos.create(Database(":memory:"))

    def _boom(*a, **kw):
        raise RuntimeError("磁盘写满")

    repos.db.insert = _boom                      # 只坏写、不坏读
    res = _rec(repos=repos).run(D, FakeBroker(positions=[], cash=1.0),
                                local_positions={}, local_cash=1.0)
    check("写库失败结论仍为通过", res.passed, str(kinds(res)))

    class BlindRepos:                            # 读不到本地账本 = 无从核对
        def __getattr__(self, _):
            raise RuntimeError("数据库锁死")

    res = Reconciler(_settings(), repos=BlindRepos()).run(
        D, FakeBroker(positions=[], cash=1.0), local_positions={}, local_cash=1.0)
    check("读库失败判不通过", not res.passed and bool(res.error), res.error)

    logger.info("\n[30] 本地视图 —— 不传 override 时从库里还原")
    repos = Repos.create(Database(":memory:"))
    repos.positions.upsert("600519.SH", volume=200, available=200, avg_cost=1650.0)
    repos.snapshots.save(D, total_asset=500_000, cash=100_000, market_value=400_000)
    broker = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 200}],
                        cash=100_000.0)
    res = _rec(repos=repos).run(D, broker)
    check("从库读持仓与现金对上", res.passed, f"{kinds(res)} {res.checked}")
    broker2 = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 100}],
                         cash=100_000.0)
    res = _rec(repos=repos).run(D, broker2)
    check("库里数量不符能发现", kinds(res) == ["POSITION_QTY"], str(kinds(res)))


# ============================================================== 人工确认
def test_acknowledge() -> None:
    logger.info("\n[31] 人工确认 —— 解除限制并留痕")
    repos = Repos.create(Database(":memory:"))
    ks = KillSwitch()
    r = _rec(repos=repos, killswitch=ks)
    broker = FakeBroker(positions=[{"symbol": "600519.SH", "volume": 200}], cash=1.0)
    r.run(D, broker, local_positions={}, local_cash=1.0)
    check("先被阻断", ks.mode is KillMode.REDUCE_ONLY)

    ok = r.acknowledge(D, operator="cgli", note="券商端确认为手工买入")
    check("确认返回成功", ok)
    check("恢复 NORMAL", ks.mode is KillMode.NORMAL, ks.mode.value)
    check("标记为人工操作", ks.manual)
    check("system_state 转为 1", repos.system.get("reconcile_ok") == "1")
    row = repos.db.query_one(
        "SELECT * FROM reconcile_logs WHERE id LIKE 'ack_%' ORDER BY created_at DESC")
    check("确认动作有独立日志", row is not None and "cgli" in str(row["detail"]))

    logger.info("\n[32] 人工确认 —— 落库失败即认为确认无效")

    class BadRepos:
        def __getattr__(self, _):
            raise RuntimeError("数据库只读")

    ks2 = KillSwitch()
    ks2.engage("对账未通过")
    r2 = Reconciler(_settings(), repos=BadRepos(), killswitch=ks2)
    check("返回 False", r2.acknowledge(D, note="试试") is False)
    check("不敢放行", ks2.mode is KillMode.REDUCE_ONLY, ks2.mode.value)


# ============================================================== 数据结构
def test_structs() -> None:
    logger.info("\n[33] 数据结构 —— 严重度决定是否阻断")
    check("CRITICAL 阻断", Discrepancy("X", severity="CRITICAL").blocking)
    check("ERROR 阻断", Discrepancy("X", severity="ERROR").blocking)
    check("WARN 不阻断", not Discrepancy("X", severity="WARN").blocking)
    check("INFO 不阻断", not Discrepancy("X", severity="INFO").blocking)
    check("未知严重度按 ERROR 处理", Discrepancy("X", severity="???").blocking)

    logger.info("\n[34] 数据结构 —— 渲染与序列化")
    res = ReconcileResult(trade_date=D)
    res.add(Discrepancy("POSITION_QTY", "600519.SH", 200, 300, "ERROR", "数量差 -100 股"))
    res.add(Discrepancy("SLIPPAGE", "000858.SZ", 155.0, 158.0, "WARN", "偏离 1.94%"))
    check("blocking 只挑严重项", len(res.blocking) == 1)
    txt = res.render()
    check("渲染含阻断提示", "阻断次日开仓" in txt)
    check("渲染含两条差异", "POSITION_QTY" in txt and "SLIPPAGE" in txt)
    dd = res.to_dict()
    check("字典可序列化", dd["passed"] is False and len(dd["discrepancies"]) == 2
          and dd["trade_date"] == "2026-08-07")

    logger.info("\n[35] 数据结构 —— 无 settings 时用默认阈值")
    r = Reconciler()
    check("默认现金容忍 1 元", r.cash_tolerance == 1.0)
    check("默认持仓零容忍", r.position_tolerance == 0)
    res = r.run(D, FakeBroker(positions=[], cash=0.0), local_positions={},
                local_cash=0.0)
    check("裸配置也能跑", res.passed)


def main() -> int:
    logger.info("=" * 64)
    logger.info("Gate-3 盘后对账 冒烟测试")
    logger.info("=" * 64)
    test_positions()
    test_cash()
    test_trades()
    test_fail_safe()
    test_integration()
    test_acknowledge()
    test_structs()
    logger.info("\n" + "=" * 64)
    logger.info(f"结果: {PASS} 通过 / {FAIL} 失败")
    logger.info("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())