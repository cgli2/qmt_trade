"""L6 运维层冒烟测试：通知 / 健康监控 / 报告。

重点验证的不是"能跑"，而是几条**运维纪律**：
- 通知通道挂了不能把主流程带崩（P4）；
- CRITICAL 不能被节流吃掉；
- 体检不通过必须真的把 KillSwitch 拉下来，而不是只发条消息；
- 报告是纯函数，同样的库产出同样的字节。
"""

from __future__ import annotations
import logging

import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt_trade.core.config import Settings                       # noqa: E402
from qmt_trade.ops import (                                      # noqa: E402
    Channel, ConsoleChannel, DailyReport, FileChannel, HealthMonitor, Level,
    MemoryChannel, Message, Notifier, Reporter, Watchdog, trading_window_guard,
)
from qmt_trade.ops.notify import WecomChannel                    # noqa: E402
from qmt_trade.risk.killswitch import KillMode, KillSwitch       # noqa: E402
from qmt_trade.storage.db import Database                        # noqa: E402
from qmt_trade.storage.models import Repos                       # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger(__name__)

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        logger.info(f"  [OK]   {name} {extra}")
    else:
        FAIL += 1
        logger.info(f"  [FAIL] {name} {extra}")
    return bool(cond)


class BoomChannel(Channel):
    """永远失败的通道，用来验证「通知挂了不影响主流程」。"""

    name = "boom"

    def __init__(self) -> None:
        self.attempts = 0

    def send(self, msg: Message) -> None:
        self.attempts += 1
        raise RuntimeError("webhook 500")


def _settings() -> Settings:
    return Settings.load()


# ==================================================================== 通知
def test_notify() -> None:
    st = _settings()

    logger.info("\n[1] 通知 —— 基本投递与字段渲染")
    mem = MemoryChannel()
    n = Notifier(st, channels=[mem])
    ok = n.notify("开仓成功", "600519.SH 买入 200 股", level="INFO",
                  价格=1688.5, 权重=0.12)
    check("投递成功", ok and len(mem.sent) == 1)
    body = mem.sent[0].render()
    check("标题与级别在正文里", "开仓成功" in body and "INFO" in body)
    check("字段被渲染", "价格" in body and "1,688.5" in body, body.replace("\n", " | "))

    logger.info("\n[2] 通知 —— 通道炸了也不能把主流程带崩（P4）")
    boom = BoomChannel()
    n2 = Notifier(st, channels=[boom])
    try:
        delivered = n2.notify("测试", "x", level="ERROR")
        crashed = False
    except Exception:
        delivered, crashed = None, True
    check("异常被吃掉", not crashed)
    check("如实返回未投递", delivered is False)
    check("失败被记录", len(n2.failures) == 1, str(n2.failures))

    logger.info("\n[3] 通知 —— 部分通道失败时仍算投递成功")
    mem3, boom3 = MemoryChannel(), BoomChannel()
    n3 = Notifier(st, channels=[boom3, mem3])
    check("有一个通道成功即成功", n3.notify("半通", level="ERROR") is True)
    check("成功通道确实收到", len(mem3.sent) == 1)

    logger.info("\n[4] 通知 —— 节流：同 key 重复告警不刷屏")
    mem4 = MemoryChannel()
    n4 = Notifier(st, channels=[mem4])
    n4.throttle_seconds = 60
    for _ in range(10):
        n4.notify("数据源超时", key="datahub:timeout", level="WARN")
    check("10 次只发 1 条", len(mem4.sent) == 1, f"实发 {len(mem4.sent)}")
    check("抑制计数正确", n4.suppressed == 9, str(n4.suppressed))

    logger.info("\n[5] 通知 —— CRITICAL 不受节流限制（刹车信号必达）")
    mem5 = MemoryChannel()
    n5 = Notifier(st, channels=[mem5])
    n5.throttle_seconds = 3600
    n5.daily_max = 1
    for _ in range(5):
        n5.critical("KillSwitch 触发", key="ks")
    check("CRITICAL 全部送达", len(mem5.sent) == 5, f"实发 {len(mem5.sent)}")

    logger.info("\n[6] 通知 —— 级别过滤与单日上限")
    mem6 = MemoryChannel()
    n6 = Notifier(st, channels=[mem6])
    n6.min_level = Level.WARN
    n6.throttle_seconds = 0
    n6.notify("调试信息", level="DEBUG")
    n6.notify("普通信息", level="INFO")
    check("低级别被过滤", len(mem6.sent) == 0)
    n6.notify("警告", level="WARN")
    check("达标级别放行", len(mem6.sent) == 1)

    mem6b = MemoryChannel()
    n6b = Notifier(st, channels=[mem6b])
    n6b.throttle_seconds = 0
    n6b.daily_max = 3
    for i in range(10):
        n6b.warn(f"告警{i}")
    check("单日上限生效", len(mem6b.sent) == 3, f"实发 {len(mem6b.sent)}")

    logger.info("\n[7] 通知 —— 关闭开关")
    mem7 = MemoryChannel()
    n7 = Notifier(st, channels=[mem7])
    n7.enabled = False
    check("关闭后不发", n7.warn("不该出现") is False and not mem7.sent)

    logger.info("\n[8] 通知 —— webhook payload 结构正确且不真发 HTTP")
    captured: list[tuple[str, dict, float]] = []
    ch = WecomChannel("http://example.invalid/hook", timeout=1.0,
                      sender=lambda u, p, t: captured.append((u, p, t)))
    n8 = Notifier(st, channels=[ch])
    n8.notify("企微测试", "正文", level="WARN")
    check("发起了一次调用", len(captured) == 1)
    check("payload 是企微 text 格式",
          captured[0][1].get("msgtype") == "text"
          and "企微测试" in captured[0][1]["text"]["content"])
    check("超时参数透传", captured[0][2] == 1.0)

    logger.info("\n[9] 通知 —— 空 URL 视为配置错误而非静默成功")
    bad = WecomChannel("", timeout=1.0, sender=lambda *a: None)
    n9 = Notifier(st, channels=[bad])
    check("未配置地址 → 投递失败", n9.notify("x", level="ERROR") is False)

    logger.info("\n[10] 通知 —— 文件通道落盘")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        fc = FileChannel(td)
        n10 = Notifier(st, channels=[fc])
        n10.warn("落盘测试", "内容")
        files = list(Path(td).glob("*.log"))
        check("生成了日志文件", len(files) == 1, str(files))
        txt = files[0].read_text(encoding="utf-8") if files else ""
        check("内容含标题且是 JSON 行", "落盘测试" in txt and txt.strip().startswith("{"))

    logger.info("\n[11] 通知 —— KillSwitch 状态变更自动播报")
    mem11 = MemoryChannel()
    n11 = Notifier(st, channels=[mem11])
    ks = KillSwitch()
    n11.bind_killswitch(ks)
    ks.engage("对账失败")
    check("切档被播报", len(mem11.sent) == 1, str(mem11.titles()))
    check("级别为 CRITICAL", mem11.sent[0].level is Level.CRITICAL)
    ks.reset()
    check("恢复也被播报", len(mem11.sent) == 2, str(mem11.titles()))

    logger.info("\n[12] 通知 —— 至少保留一个出口（配置全错也不能没嘴）")
    n12 = Notifier(st.merged({"ops": {"notify": {"channels": ["不存在的通道"]}}}))
    check("兜底 console 通道", len(n12.channels) == 1
          and isinstance(n12.channels[0], ConsoleChannel))


# ================================================================ 健康监控
def _repos_with_data(*, snapshot_date: date | None = None,
                     reconcile_passed: bool = True,
                     reject_ratio: float = 0.0) -> Repos:
    repos = Repos.create(Database(":memory:"))
    d = snapshot_date or date.today()
    for i in range(3):
        day = d - timedelta(days=2 - i)
        repos.snapshots.save(day, total_asset=1_000_000 * (1 + 0.01 * i),
                             cash=500_000, market_value=500_000,
                             position_count=2, regime="RANGE")
    repos.db.insert("reconcile_logs", {
        "id": "rc1", "trade_date": d.isoformat(),
        "passed": 1 if reconcile_passed else 0,
        "detail": "" if reconcile_passed else "现金差 1234 元",
        "created_at": time.time()})
    total = 10
    n_bad = int(round(total * reject_ratio))
    for i in range(total):
        repos.orders.create(idempotency_key=f"k{i}", symbol="600000.SH", side="BUY",
                            volume=100, price=10.0, trade_date=d,
                            status="REJECTED" if i < n_bad else "FILLED")
    return repos


def test_monitor() -> None:
    st = _settings()

    logger.info("\n[13] 体检 —— 全绿时不降级")
    mem = MemoryChannel()
    ks = KillSwitch()
    mon = HealthMonitor(st, repos=_repos_with_data(), killswitch=ks,
                        notifier=Notifier(st, channels=[mem]))
    rep = mon.check()
    check("整体健康", rep.healthy, "; ".join(r.render() for r in rep.failed))
    check("未降级", not rep.degraded and ks.mode is KillMode.NORMAL)

    logger.info("\n[14] 体检 —— 对账不过必须拉闸（不是只发消息）")
    ks2 = KillSwitch()
    mem2 = MemoryChannel()
    mon2 = HealthMonitor(st, repos=_repos_with_data(reconcile_passed=False),
                         killswitch=ks2, notifier=Notifier(st, channels=[mem2]))
    rep2 = mon2.check()
    r = rep2.get("reconcile")
    check("对账项标红", r is not None and not r.ok and r.level is Level.CRITICAL)
    check("KillSwitch 被拉到 REDUCE_ONLY", ks2.mode is KillMode.REDUCE_ONLY,
          ks2.mode.value)
    check("降级原因写进报告", rep2.degraded and any("reconcile" in x
                                                for x in rep2.degrade_reasons))
    check("推送了 CRITICAL", mem2.sent and mem2.sent[-1].level is Level.CRITICAL)

    logger.info("\n[15] 体检 —— 数据停更要停开仓")
    ks3 = KillSwitch()
    old = date.today() - timedelta(days=10)
    mon3 = HealthMonitor(st, repos=_repos_with_data(snapshot_date=old), killswitch=ks3)
    rep3 = mon3.check(notify=False)
    r3 = rep3.get("data_freshness")
    check("识别出数据落后", r3 is not None and not r3.ok, r3.message if r3 else "")
    check("触发降级", ks3.mode is KillMode.REDUCE_ONLY)

    logger.info("\n[16] 体检 —— 下单被拒比例过高要报警")
    mon4 = HealthMonitor(st, repos=_repos_with_data(reject_ratio=0.5))
    rep4 = mon4.check(notify=False)
    r4 = rep4.get("order_health")
    check("识别出异常比例", r4 is not None and not r4.ok, r4.message if r4 else "")
    check("比例计算正确", r4 is not None and abs(r4.detail.get("ratio", 0) - 0.5) < 1e-9)

    logger.info("\n[17] 体检 —— 检查项自身抛异常 = 不健康（未知即危险）")
    mon5 = HealthMonitor(st)
    mon5.register("boom", lambda: (_ for _ in ()).throw(RuntimeError("坏了")))
    rep5 = mon5.check(notify=False)
    check("异常项被记为失败", any(not r.ok and "坏了" in r.message for r in rep5.results),
          "; ".join(r.render() for r in rep5.results))
    check("其余检查照常完成", len(rep5.results) >= 7, str(len(rep5.results)))

    logger.info("\n[18] 体检 —— LLM 超预算只降级不停开仓（P5）")
    repos6 = _repos_with_data()
    budget = float(st.get("llm.budget.daily_cny", 30.0))
    repos6.llm_calls.save(prompt_hash="h1", model="deepseek-chat", prompt="p",
                          response="r", cost_cny=budget * 1.2, trade_date=date.today())
    ks6 = KillSwitch()
    mon6 = HealthMonitor(st, repos=repos6, killswitch=ks6)
    rep6 = mon6.check(notify=False)
    r6 = rep6.get("llm_budget")
    check("识别超预算", r6 is not None and not r6.ok, r6.message if r6 else "")
    check("但不拉闸（纯因子继续跑）", ks6.mode is KillMode.NORMAL, ks6.mode.value)

    logger.info("\n[19] 心跳与看门狗")
    mon7 = HealthMonitor(st)
    mon7.heartbeat_seconds = 0.05
    wd = Watchdog("intraday", timeout_seconds=0.05, monitor=mon7)
    wd.tick()
    check("刚 tick 未过期", not wd.expired())
    check("心跳被登记", mon7._check_heartbeats().ok)
    time.sleep(0.08)
    check("超时后过期", wd.expired())
    check("心跳项转红", not mon7._check_heartbeats().ok)
    check("看门狗可转成检查项", not wd.as_check().ok and wd.as_check().level is Level.ERROR)

    logger.info("\n[20] 定时 KillSwitch —— 非交易时段自动停开仓")
    ks7 = KillSwitch()
    check("盘中不动",
          trading_window_guard(datetime(2026, 8, 7, 10, 30), ks7) is True
          and ks7.mode is KillMode.NORMAL)
    check("收盘后停开仓",
          trading_window_guard(datetime(2026, 8, 7, 15, 30), ks7) is False
          and ks7.mode is KillMode.REDUCE_ONLY)
    ks8 = KillSwitch()
    check("周末停开仓",
          trading_window_guard(datetime(2026, 8, 8, 10, 30), ks8) is False
          and ks8.mode is KillMode.REDUCE_ONLY)


# ==================================================================== 报告
def test_report() -> None:
    st = _settings()
    d = date(2026, 8, 7)
    repos = Repos.create(Database(":memory:"))
    equities = [1_000_000, 1_012_000, 995_000, 1_030_000]
    for i, eq in enumerate(equities):
        repos.snapshots.save(d - timedelta(days=3 - i), total_asset=eq,
                             cash=eq * 0.4, market_value=eq * 0.6,
                             position_count=2, regime="TREND_UP")
    repos.trades.add(symbol="600519.SH", side="BUY", price=1688.0, volume=100,
                     amount=168_800, total_cost=92.0, trade_date=d)
    repos.trades.add(symbol="000001.SZ", side="SELL", price=12.5, volume=1000,
                     amount=12_500, total_cost=18.0, realized_pnl=1_240.0, trade_date=d)
    repos.positions.upsert("600519.SH", volume=100, available=0, avg_cost=1688.0,
                           last_price=1720.0, stop_loss_price=1580.0,
                           industry="食品饮料")
    repos.orders.create(idempotency_key="k1", symbol="600519.SH", side="BUY",
                        volume=100, price=1688.0, trade_date=d, status="FILLED")
    repos.orders.create(idempotency_key="k2", symbol="300750.SZ", side="BUY",
                        volume=100, price=200.0, trade_date=d, status="GUARD_BLOCKED")
    repos.risk_events.add("GATE1", "MAX_INDUSTRY_WEIGHT", "行业超配", trade_date=d,
                          severity="WARN", symbol="600519.SH")
    repos.llm_calls.save(prompt_hash="h", model="deepseek-chat", prompt="p",
                         response="r", cost_cny=3.75, trade_date=d)

    logger.info("\n[21] 日报 —— 核心指标正确")
    rpt = Reporter(st, repos=repos)
    rep = rpt.daily(d, health={"healthy": True, "degraded": False},
                    lessons=["止损略紧，可放宽至 1.5×ATR"])
    check("权益正确", abs(rep.equity - 1_030_000) < 1e-6, str(rep.equity))
    check("当日收益正确",
          rep.day_return is not None and abs(rep.day_return - (1_030_000 / 995_000 - 1)) < 1e-9,
          f"{rep.day_return:.4%}")
    check("累计收益正确",
          rep.total_return is not None and abs(rep.total_return - 0.03) < 1e-9,
          f"{rep.total_return:.4%}")
    check("最大回撤为负数表达",
          rep.max_drawdown is not None and rep.max_drawdown < 0,
          f"{rep.max_drawdown:.4%}")
    check("成交与费用汇总", len(rep.trades) == 2 and abs(rep.fees - 110.0) < 1e-9,
          f"fees={rep.fees}")
    check("已实现盈亏汇总", abs(rep.realized_pnl - 1240.0) < 1e-9)
    check("LLM 成本入账", abs(rep.llm_cost - 3.75) < 1e-9)

    logger.info("\n[22] 日报 —— Markdown 完整可读")
    md = rep.to_markdown()
    for kw in ("交易日报", "总权益", "600519.SH", "MAX_INDUSTRY_WEIGHT", "复盘经验"):
        check(f"包含「{kw}」", kw in md)
    check("表格行数合理", md.count("\n|") >= 10, str(md.count("\n|")))

    logger.info("\n[23] 日报 —— 精简文本适合推消息")
    txt = rep.to_text()
    check("首行是概览", txt.splitlines()[0].startswith("【日报"))
    check("长度可控（企微友好）", len(txt) < 500, f"len={len(txt)}")

    logger.info("\n[24] 日报 —— 纯函数可复现（P6）")
    a = Reporter(st, repos=repos).daily(d).to_markdown()
    b = Reporter(st, repos=repos).daily(d).to_markdown()
    strip = lambda s: "\n".join(x for x in s.splitlines() if not x.startswith("_生成于"))
    check("两次生成完全一致", strip(a) == strip(b))

    logger.info("\n[25] 日报 —— 异常必须浮到最上面")
    repos.db.insert("reconcile_logs", {"id": "rc", "trade_date": d.isoformat(),
                                       "passed": 0, "detail": "持仓差 100 股",
                                       "created_at": time.time()})
    repos.system.set("killswitch", "REDUCE_ONLY", "对账失败")
    rep2 = Reporter(st, repos=repos).daily(d)
    check("对账告警", any("对账" in w for w in rep2.warnings), str(rep2.warnings))
    check("KillSwitch 告警", any("KillSwitch" in w for w in rep2.warnings))
    check("告警在 Markdown 顶部",
          rep2.to_markdown().index("需要人工确认") < rep2.to_markdown().index("## 账户"))

    logger.info("\n[26] 日报 —— 空数据不崩")
    empty = Reporter(st, repos=Repos.create(Database(":memory:"))).daily(d)
    check("空库能生成", isinstance(empty.to_markdown(), str) and len(empty.to_markdown()) > 50)
    check("空仓文案", "_空仓_" in empty.to_markdown())
    check("无数据库也能生成", isinstance(Reporter(st).daily(d).to_markdown(), str))

    logger.info("\n[27] 周报 —— 区间统计")
    wk = Reporter(st, repos=repos).weekly(d, days=7,
                                          pool_weights={"momentum": 0.4, "__cash__": 0.6})
    check("期初期末正确",
          abs(wk.equity_start - 1_000_000) < 1e-6 and abs(wk.equity_end - 1_030_000) < 1e-6)
    check("区间收益正确", abs(wk.ret - 0.03) < 1e-9, f"{wk.ret:.4%}")
    check("胜率有值", wk.win_rate == 1.0, str(wk.win_rate))
    md_w = wk.to_markdown()
    check("周报含策略池权重", "momentum" in md_w and "40.0%" in md_w)

    logger.info("\n[28] 报告落盘与推送")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mem = MemoryChannel()
        r3 = Reporter(st, repos=repos, notifier=Notifier(st, channels=[mem]))
        r3.output_dir = Path(td)
        p = r3.save(r3.daily(d))
        check("文件已生成", p.exists() and p.stat().st_size > 100, str(p))
        rep_w = r3.daily(d)
        check("推送成功", r3.push(rep_w) is True)
        check("有告警时级别抬高",
              mem.sent[-1].level is Level.WARN if rep_w.warnings else True,
              mem.sent[-1].level.name)


def main() -> int:
    logger.info("=" * 60)
    logger.info("L6 运维层冒烟测试")
    logger.info("=" * 60)
    test_notify()
    test_monitor()
    test_report()
    logger.info("\n" + "=" * 46)
    logger.info(f"通过 {PASS} / 失败 {FAIL}")
    logger.info("=" * 46)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())