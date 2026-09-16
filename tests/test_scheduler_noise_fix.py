"""调度日志噪音三处修复的回归测试。

对应缺陷：backend.log 里每 30 秒刷两条一模一样的

    Execution of job "TradingScheduler._guarded_fire (trigger: interval[0:00:30],
    next run at: 2026-09-16 00:06:01 CST)" skipped: maximum number of running
    instances reached (1)

而且时间戳是 **00:05** —— 凌晨，根本不在任何巡检窗口内。三处根因、三处修复：

1. APScheduler 路径漏判窗口。``IntervalTrigger`` 只认"每 N 秒"，``start_time`` /
   ``end_time`` 传不进去（``start_date``/``end_date`` 是一次性时间点，表达不了每日
   复现的窗口），于是三个 30 秒巡检任务夜里照触发 —— 每 tick 抢一次策略边界锁、
   跑一遍 ``apply_at_boundary``（DuckDB 写）。修复：``JobSpec.in_window`` +
   ``_guarded_fire(enforce_window=True)`` 入口静默跳过。见 [1]/[1b]/[2]。
2. ``@job`` 的策略边界锁**无限排队**。锁是全任务共享的，长任务持锁数分钟时三个巡检
   任务全阻塞在锁上，APScheduler 判"上一实例还在跑"，每 tick 刷一条告警。
   修复：``acquire(timeout=_lock_budget(...))`` + 让路 SKIP + 限流告警。见 [4]/[5]。
   预算**不能写死**：写死 5s 时 ``intraday`` 与持锁 6~13s 的 ``etf_t0_intraday``
   同为 30 秒周期、相位锁死，实测 13 轮让路 12 轮 —— 噪音没了，CRITICAL 的持仓
   守护却被安静地饿死。故预算按任务自身周期推导，见 [4]/[4b]/[4c]。
3. 告警归不到任务 + 裸输出。所有任务共用回调 ``_guarded_fire``，不设 ``name`` 时
   APScheduler 用回调限定名（[3] 有实证），三条告警完全无法区分；且 ``apscheduler``
   这个 logger 没有 handler，WARNING 经 ``logging.lastResort`` 裸打 stderr，没有时间
   戳/级别/来源。修复：``name=spec.name`` + ``THIRD_PARTY_LOGGERS`` 归并。见 [3]/[6]。

这三处失效的方式都是**只产生噪音、不报错**：任务照跑、退出码照 0、UI 一切正常，
唯一症状是日志被刷爆、真告警被淹没。只能靠断言锁住。

不写盘、不联网、不碰 DuckDB、不启动真实调度线程（APScheduler 以 ``paused`` 态验证，
job 已进 jobstore、命名与生产完全一致，但永不触发）。

运行：python tests/test_scheduler_noise_fix.py
"""

from __future__ import annotations

import contextlib
import io
import logging
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from types import SimpleNamespace

# Windows 控制台默认 GBK，中文断言名会乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt_trade.core.config import Settings                             # noqa: E402
from qmt_trade.core.logging import (LOG_FORMAT, THIRD_PARTY_LOGGERS,   # noqa: E402
                                    setup_logging)
from qmt_trade.core.strategies import STANDALONE_STRATEGIES            # noqa: E402
from qmt_trade.scheduler import JobSpec, TradingScheduler              # noqa: E402
from qmt_trade.scheduler import jobs as J                              # noqa: E402
from qmt_trade.scheduler.jobs import job                               # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger(__name__)

PASS = FAIL = 0

#: 用户日志里的原始时刻：2026-09-16 00:05:48（凌晨，任何巡检窗口之外）
NIGHT = datetime(2026, 9, 16, 0, 5, 48)
#: 用户日志里 ``interval[0:00:30]`` 对应的三个巡检任务
INTRADAY_JOBS = ("intraday", "etf_t0_intraday", "stock_t0_intraday")


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        logger.info("  [OK]   %s %s", name, extra)
    else:
        FAIL += 1
        logger.info("  [FAIL] %s %s", name, extra)
    return bool(cond)


class _Cap(logging.Handler):
    """把日志记录抓到内存里，用于断言"该打的打了、不该打的没打"。"""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, min_level: int = logging.WARNING) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= min_level]


# ------------------------------------------------------------------ 脚手架
def _settings(**strategies) -> Settings:
    """最小可用配置。``env_overlay=False``：不许本机 ``QMT_*__*`` 环境变量干扰断言。"""
    return Settings({
        "scheduler": {"enabled": True, "timezone": "Asia/Shanghai",
                      "misfire_grace_seconds": 900, "jobs": {}},
        "strategies": {sid: {"enabled": bool(v)} for sid, v in strategies.items()},
    }, env_overlay=False)


def _specs(**strategies) -> list[JobSpec]:
    """按配置生成**生产同款**计划表。

    用 ``__new__`` 绕过 ``__init__``：``_build_specs`` 只读 ``jobs_cfg`` 与
    ``settings``，不需要 runner/ctx/线程，也就不需要碰 DuckDB。
    """
    s = TradingScheduler.__new__(TradingScheduler)
    s.settings = _settings(**strategies)
    s.jobs_cfg = {}
    return s._build_specs()                                    # noqa: SLF001


def _all_enabled_specs() -> list[JobSpec]:
    """全部独立策略都启用 → 每条计划都挂得上日程。"""
    return _specs(**{sid: True for sid in STANDALONE_STRATEGIES})


def _bare_sched(specs: list[JobSpec]):
    """只装好 ``_guarded_fire`` 所需字段的调度器，``_fire`` 换成记录器。

    ``_guarded_fire`` 只经 ``self._fire`` 调任务体，覆盖成实例属性即可拦住真实执行
    —— 本节要断言的恰恰是"有没有被触发"，不是任务跑成什么样。
    """
    s = TradingScheduler.__new__(TradingScheduler)
    s.specs = specs
    s.runner = None
    rec: list[str] = []
    s._fire = rec.append                                       # noqa: SLF001
    return s, rec


def _window_excluding_now() -> tuple[dtime, dtime]:
    """造一个必定不含"现在"的窗口。

    跨午夜时 ``a > b``，此时 ``a <= t <= b`` 对任何 t 恒 False —— 退化区间同样
    不含现在，正好可用。
    """
    a = (datetime.now() + timedelta(hours=2)).time()
    b = (datetime.now() + timedelta(hours=3)).time()
    return (a, b) if a <= b else (b, a)


@contextlib.contextmanager
def _captured(logger_name: str):
    """临时独占某个 logger 的输出（关掉向上传播，免得重复计数）。"""
    lg = logging.getLogger(logger_name)
    cap = _Cap()
    lg.addHandler(cap)
    prev_level, prev_prop = lg.level, lg.propagate
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    try:
        yield cap
    finally:
        lg.removeHandler(cap)
        lg.setLevel(prev_level)
        lg.propagate = prev_prop


# ============================================================ [1] 窗口判定
def test_in_window() -> None:
    logger.info("\n[1] JobSpec.in_window：用户日志里的 00:05:48 必须在窗口外")
    spec = JobSpec("stock_t0_intraday", "interval", seconds=30,
                   start_time=dtime(9, 30), end_time=dtime(15, 0))
    check("凌晨 00:05:48 在窗口外", not spec.in_window(NIGHT), str(NIGHT.time()))
    check("盘中 10:00 在窗口内", spec.in_window(datetime(2026, 9, 16, 10, 0)))
    check("起边界 09:30 含端点", spec.in_window(datetime(2026, 9, 16, 9, 30)))
    check("止边界 15:00 含端点", spec.in_window(datetime(2026, 9, 16, 15, 0)))
    check("15:00:01 已出窗口", not spec.in_window(datetime(2026, 9, 16, 15, 0, 1)))
    check("cron 型恒 True（时刻由触发器自己表达）",
          JobSpec("data_sync", "cron", hour=6, minute=30).in_window(NIGHT))
    check("未配窗口的 interval 恒 True（不该误伤）",
          JobSpec("intraday", "interval", seconds=3).in_window(NIGHT))


def test_production_specs_have_window() -> None:
    logger.info("\n[1b] 生产计划表：三个 30 秒巡检任务都配了窗口（否则门禁形同虚设）")
    specs = {s.name: s for s in _specs()}
    missing = [n for n in INTRADAY_JOBS if n not in specs]
    check("三个巡检任务都在计划表里", not missing, str(missing))
    for n in INTRADAY_JOBS:
        sp = specs.get(n)
        if sp is None:
            continue
        check(f"{n} 配了巡检窗口", bool(sp.start_time and sp.end_time),
              f"{sp.start_time}–{sp.end_time}")
        check(f"{n} 凌晨 00:05 在窗口外", not sp.in_window(NIGHT))
    check("用户日志的 interval[0:00:30] 确实来自 30 秒巡检",
          any(specs[n].seconds == 30 for n in INTRADAY_JOBS),
          str({n: specs[n].seconds for n in INTRADAY_JOBS}))
    cron_win = [s.name for s in specs.values()
                if s.kind == "cron" and (s.start_time or s.end_time)]
    check("cron 任务不该配窗口（配了也不会被用，属误导性配置）", not cron_win, str(cron_win))


# ====================================================== [2] 触发入口门禁
def test_guarded_fire_window_gate() -> None:
    logger.info("\n[2] _guarded_fire：窗口外静默跳过（不触发任务体、不刷 INFO）")
    ws, we = _window_excluding_now()
    out_spec = JobSpec("probe_intraday", "interval", seconds=30,
                       start_time=ws, end_time=we)
    check("前置：该窗口确实不含现在", not out_spec.in_window(datetime.now()),
          f"{ws}–{we}")

    with _captured("qmt_trade.scheduler") as cap:
        sched, rec = _bare_sched([out_spec])
        sched._guarded_fire("probe_intraday")                    # noqa: SLF001
        check("窗口外不触发任务体", rec == [], str(rec))
        check("窗口外只留 DEBUG（不再每 30 秒刷一行）",
              bool(cap.records) and all(r.levelno < logging.INFO for r in cap.records),
              str([r.levelname for r in cap.records]))

    # 补跑路径（机器休眠错过整段窗口）必须绕过窗口门禁，否则补救永久失效
    sched, rec = _bare_sched([out_spec])
    sched._guarded_fire("probe_intraday", enforce_window=False)  # noqa: SLF001
    check("enforce_window=False 时补跑照常触发（_on_job_missed 依赖此语义）",
          rec == ["probe_intraday"], str(rec))

    in_spec = JobSpec("probe_open", "interval", seconds=30,
                      start_time=dtime(0, 0), end_time=dtime(23, 59, 59))
    check("前置：全天窗口确实含现在", in_spec.in_window(datetime.now()))
    sched, rec = _bare_sched([in_spec])
    sched._guarded_fire("probe_open")                            # noqa: SLF001
    check("窗口内正常触发", rec == ["probe_open"], str(rec))

    # 找不到 spec（reload 后 job 尚未摘干净的残留触发）不该被窗口门禁吞掉
    sched, rec = _bare_sched([out_spec])
    sched._guarded_fire("ghost_job")                             # noqa: SLF001
    check("specs 里没有的任务名照常交给 _fire（残留触发不被静默吞掉）",
          rec == ["ghost_job"], str(rec))

    sched, rec = _bare_sched([JobSpec("data_sync", "cron", hour=6, minute=30)])
    sched._guarded_fire("data_sync")                             # noqa: SLF001
    check("cron 任务不受窗口门禁影响", rec == ["data_sync"], str(rec))


# ====================================================== [3] 告警可归因
def test_apscheduler_job_name() -> None:
    logger.info("\n[3] APScheduler job.name：告警能归到具体任务，不再三条一模一样")
    specs = _all_enabled_specs()
    sched = TradingScheduler.__new__(TradingScheduler)
    sched.timezone = "Asia/Shanghai"
    sched.misfire = 900
    sched.specs = specs
    sched._sched = None                                          # noqa: SLF001
    ap = sched._build_apscheduler()                              # noqa: SLF001
    if not check("APScheduler 可用", ap is not None):
        return
    try:
        ap.start(paused=True)          # paused：job 已进 jobstore 但永不触发
        jobs = {j.id: j.name for j in ap.get_jobs()}
        enabled = {s.name for s in specs if s.enabled}
        check("启用任务全部挂上日程", enabled == set(jobs),
              str(sorted(enabled ^ set(jobs))))
        mismatch = {i: n for i, n in jobs.items() if n != i}
        check("每个 job.name == 任务名（告警可归因）", not mismatch, str(mismatch))
        check("告警文案里不再出现回调限定名",
              not any("_guarded_fire" in n for n in jobs.values()),
              str(sorted(jobs.values()))[:200])

        # 实证 APScheduler 的默认取名规则：不传 name 时用回调限定名。三个巡检任务
        # 共用 _guarded_fire，于是 str(job) 完全一样 —— 正是用户日志里那串。
        probe = JobSpec("stock_t0_intraday", "interval", seconds=30,
                        start_time=dtime(9, 30), end_time=dtime(15, 0))
        ap.add_job(sched._guarded_fire, sched._trigger_of(probe),   # noqa: SLF001
                   args=[probe.name], id="__probe_noname")
        no_name_job = ap.get_job("__probe_noname")
        check("不传 name 时 APScheduler 用回调限定名（缺陷复现）",
              no_name_job.name.endswith("_guarded_fire")
              and no_name_job.name != probe.name, repr(no_name_job.name))
        check("缺陷复现的 str(job) 形状与用户日志一致",
              str(no_name_job).startswith(no_name_job.name + " (trigger: interval"),
              str(no_name_job)[:120])
        check("修复后同名任务的 str(job) 带任务名",
              str(ap.get_job(probe.name)).startswith(probe.name + " (trigger:"),
              str(ap.get_job(probe.name))[:120])
        ap.remove_job("__probe_noname")
    finally:
        ap.shutdown(wait=False)


# ============================================== [4] 边界锁：预算推导与有界等待
class _ProbeRunner:
    """``JobRunner`` 的最小替身：只提供 ``@job`` 包装器真正回调的成员。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.history: list = []
        self.beats: list[str] = []
        self.recorded: list = []
        self.failures: list = []
        self.body_calls = 0

    def _beat(self, name):
        self.beats.append(name)

    def _record(self, res):
        self.recorded.append(res)

    def _on_failure(self, res, critical=None):
        self.failures.append(res)

    @job("probe_job")
    def probe(self):
        self.body_calls += 1
        return {"ran": True}


@contextlib.contextmanager
def _lock_held_by_other_thread(lock, timeout: float = 20.0):
    """让**另一个线程**持有边界锁。

    必须是别的线程：``_strategy_boundary_lock`` 是 RLock，同线程重入会直接成功，
    那样根本走不到"抢不到锁"的分支，测试会假绿。
    """
    ready, release = threading.Event(), threading.Event()

    def _hold():
        with lock:
            ready.set()
            release.wait(timeout)

    t = threading.Thread(target=_hold, name="lock-holder", daemon=True)
    t.start()
    if not ready.wait(timeout):
        raise RuntimeError("持锁线程未就绪")
    try:
        yield
    finally:
        release.set()
        t.join(timeout)


@contextlib.contextmanager
def _lock_held_for(lock, hold: float):
    """让**另一个线程**持锁 ``hold`` 秒后自动释放。

    与 ``_lock_held_by_other_thread`` 的区别：那个持到上下文退出（用来断言"抢不到
    → 让路"），这个持固定时长（用来断言"预算内等得起 → 不该让路"）。持锁方就绪后
    才 yield，否则主线程可能先抢到锁，[4c] 会假绿。
    """
    acquired, done = threading.Event(), threading.Event()

    def _hold():
        with lock:
            acquired.set()
            done.wait(hold)          # 到点自动释放；finally 的 set 只是提前放行

    t = threading.Thread(target=_hold, name="brief-lock-holder", daemon=True)
    t.start()
    if not acquired.wait(hold + 5):
        raise RuntimeError("持锁线程未就绪")
    try:
        yield
    finally:
        done.set()
        t.join(hold + 5)


@contextlib.contextmanager
def _stub_boundary(calls: list):
    """把 ``StrategyRepository.apply_at_boundary`` 换成记录器。

    ``@job`` 在锁内先跑这一步（DuckDB 写）。替身让本节既不需要真库，又能断言
    "锁内语义没被改坏"。
    """
    import qmt_trade.storage.strategies as st
    orig = st.StrategyRepository

    class _Repo:
        def __init__(self, db):
            pass

        def apply_at_boundary(self, ctx):
            calls.append(ctx)

    st.StrategyRepository = _Repo
    try:
        yield
    finally:
        st.StrategyRepository = orig


def _try_acquire(lock, timeout: float = 1.0) -> bool:
    got = lock.acquire(timeout=timeout)
    if got:
        lock.release()
    return got


#: 实测值（backend.log 2026-09-16 09:30~09:36 段）：``etf_t0_intraday`` 开盘时段
#: 连续 13 轮持锁 6.7~13.2s，全量 p95 12.64s。预算必须明显大过它，否则与它同为
#: 30 秒周期、相位锁死的 ``intraday`` 每轮都超时让路 —— 那正是写死 5s 时的回归。
LOCK_HOLD_P95 = 12.64
#: 生产计划表里三个巡检任务的真实周期（``config/settings.yaml`` 实测均为 30s）
LIVE_INTERVAL = 30


class _CfgStub:
    """``Settings`` 的零 IO 替身：只喂 ``_lock_budget`` 回落路径要读的那一个键。"""

    def __init__(self, **kv):
        self._kv = kv

    def get(self, key, default=None):
        return self._kv.get(key, default)


def _budget_of(name: str, *, intervals=None, settings=None) -> float:
    return J._lock_budget(SimpleNamespace(
        ctx=SimpleNamespace(_job_intervals=intervals or {}, settings=settings)), name)


def test_lock_budget_derivation() -> None:
    logger.info("\n[4] 等锁预算按任务自身周期推导（写死一个数必然顾此失彼）")
    check("等待比例在 (0,1)：预算必须明显小于自身周期",
          0 < J.BOUNDARY_LOCK_WAIT_RATIO < 1, str(J.BOUNDARY_LOCK_WAIT_RATIO))
    check("预算上限 < 最小巡检周期 30s（等锁+干活不会跨到下轮触发）",
          J.BOUNDARY_LOCK_TIMEOUT_MAX < LIVE_INTERVAL,
          f"{J.BOUNDARY_LOCK_TIMEOUT_MAX}s")
    check("cron 型预算 > interval 型上限（一天一轮，让路等于这次机会没了）",
          J.BOUNDARY_LOCK_TIMEOUT_CRON > J.BOUNDARY_LOCK_TIMEOUT_MAX,
          f"{J.BOUNDARY_LOCK_TIMEOUT_CRON}s vs {J.BOUNDARY_LOCK_TIMEOUT_MAX}s")

    for name in INTRADAY_JOBS:
        b = _budget_of(name, intervals={name: LIVE_INTERVAL})
        check(f"{name}: 预算够长，等得完实测持锁 p95 {LOCK_HOLD_P95}s（不饿死）",
              b > LOCK_HOLD_P95, f"{b:.2f}s")
        check(f"{name}: 预算 < 自身周期 {LIVE_INTERVAL}s（不跨轮）",
              b < LIVE_INTERVAL, f"{b:.2f}s")

    # 权威值必须是触发器周期，不是配置：工作台改了周期而调度器尚未 reload 时，
    # 按新配置算出的预算可能长过真实周期 → MaxInstances 噪音会回来。
    check("优先取调度器发布的真实周期（ctx._job_intervals 胜过配置）",
          _budget_of("intraday", intervals={"intraday": LIVE_INTERVAL},
                     settings=_CfgStub(**{"scheduler.jobs.intraday_interval_seconds": 5}))
          == _budget_of("intraday", intervals={"intraday": LIVE_INTERVAL}),
          "配 5s 仍按 30s 算")
    check("ctx 没有发布值时回落配置",
          _budget_of("intraday", settings=_CfgStub(
              **{"scheduler.jobs.intraday_interval_seconds": 10})) == 6.0,
          f"{_budget_of('intraday', settings=_CfgStub(**{'scheduler.jobs.intraday_interval_seconds': 10})):.2f}s")
    check("cron 型（计划表外）用 cron 预算",
          _budget_of("tail_pick_select") == J.BOUNDARY_LOCK_TIMEOUT_CRON,
          f"{_budget_of('tail_pick_select'):.2f}s")
    check("未知任务也用 cron 预算（不会拿到 0 而永久让路）",
          _budget_of("probe_job") == J.BOUNDARY_LOCK_TIMEOUT_CRON)

    # 两个边界：周期极小时预算不能被算成 0（0 = 每次必让路），极大时不能超上限。
    check("周期 1s → 预算取下限 1.0s，不会退化成 0（0 等于永久让路）",
          _budget_of("intraday", intervals={"intraday": 1}) >= 1.0,
          f"{_budget_of('intraday', intervals={'intraday': 1}):.2f}s")
    check("周期 1h → 预算封顶在上限（不会等到跨轮）",
          _budget_of("intraday", intervals={"intraday": 3600})
          == J.BOUNDARY_LOCK_TIMEOUT_MAX,
          f"{_budget_of('intraday', intervals={'intraday': 3600}):.2f}s")


def test_boundary_lock_bounded_wait() -> None:
    logger.info("\n[4b] 抢不到锁就让路，而不是无限排队")
    ctx = SimpleNamespace(
        _strategy_boundary_lock=threading.RLock(),
        repos=SimpleNamespace(db=None))
    runner = _ProbeRunner(ctx)
    yielded: list = []
    budget = 0.3               # 让路分支要真跑到，但测试不该等 18 秒
    orig_budget = J._lock_budget
    J._lock_budget = lambda r, n: budget     # noqa: ARG005
    try:
        with _stub_boundary(yielded), \
                _lock_held_by_other_thread(ctx._strategy_boundary_lock):
            t0 = time.perf_counter()
            res = runner.probe()
            waited = time.perf_counter() - t0
            check("锁被占用时不进边界（apply_at_boundary 未调用）", yielded == [],
                  str(len(yielded)))
        check("抢不到锁 → SKIP，而不是抛异常或卡死", res.skipped, res.render())
        check("任务体没有执行", runner.body_calls == 0, str(runner.body_calls))
        check("SKIP 原因写明是让路", "让路" in res.reason, res.reason)
        check("SKIP 原因带上本轮预算（便于判断是不是预算给小了）",
              f"{budget:.0f}s" in res.reason, res.reason)
        check("装饰器按 _lock_budget 取预算，而非写死常量",
              budget * 0.6 <= waited < budget * 3,
              f"{waited:.2f}s，预算 {budget}s")
        check("心跳照打（体检不会把让路误读成任务失联）",
              runner.beats == ["probe_job"], str(runner.beats))
        check("SKIP 已留痕（UI 能看见这次让路）",
              runner.recorded == [res] and runner.history == [res])
        check("让路没有误判为失败（不会触发 CRITICAL 拉闸）",
              runner.failures == [], str(runner.failures))
        check("让路分支没有泄漏 acquire（锁已可被正常获取）",
              _try_acquire(ctx._strategy_boundary_lock))

        # 反向守卫：无锁竞争时一切照旧，别把正常路径也一起改坏
        runner2 = _ProbeRunner(ctx)
        ran: list = []
        with _stub_boundary(ran):
            res2 = runner2.probe()
        check("无锁竞争时任务体照常执行",
              res2.ok and not res2.skipped and runner2.body_calls == 1, res2.render())
        check("锁内仍先跑 apply_at_boundary（边界语义未变）", len(ran) == 1,
              str(len(ran)))
        check("任务执行完锁已释放（不会把后续任务全堵死）",
              _try_acquire(ctx._strategy_boundary_lock, timeout=0.2))
    finally:
        J._lock_budget = orig_budget


def test_no_starvation_under_lock_hold() -> None:
    logger.info("\n[4c] 持锁时长在预算内时必须照跑（写死 5s 时被饿死的回归）")
    ctx = SimpleNamespace(
        _strategy_boundary_lock=threading.RLock(),
        repos=SimpleNamespace(db=None),
        _job_intervals={"probe_job": LIVE_INTERVAL})
    runner = _ProbeRunner(ctx)
    b = J._lock_budget(runner, "probe_job")
    check("探针任务按 30s 周期取到预算", b > LOCK_HOLD_P95, f"{b:.2f}s")

    ran: list = []
    hold = 0.5                       # 缩短版持锁：只要 < 预算就必须等到并跑成
    with _stub_boundary(ran), \
            _lock_held_for(ctx._strategy_boundary_lock, hold):
        t0 = time.perf_counter()
        res = runner.probe()
        waited = time.perf_counter() - t0
    check("持锁方占用期间不让路（排队等到锁）", not res.skipped, res.render())
    check("任务体照常执行", runner.body_calls == 1, str(runner.body_calls))
    check("锁内仍先跑 apply_at_boundary", len(ran) == 1, str(len(ran)))
    check("确实等了持锁方释放（不是碰巧抢到）", waited >= hold * 0.8,
          f"{waited:.2f}s，持锁 {hold}s")
    check("等锁+干活总时长 < 自身周期（不会跨到下轮触发）",
          waited < LIVE_INTERVAL, f"{waited:.2f}s")


def test_publish_intervals() -> None:
    logger.info("\n[4d] 调度器把真实触发周期发布给 jobs 侧（预算的权威来源）")
    specs = _specs()
    ctx = SimpleNamespace()
    s = TradingScheduler.__new__(TradingScheduler)
    s.runner = SimpleNamespace(ctx=ctx)
    s._publish_intervals(specs)                             # noqa: SLF001
    pub = getattr(ctx, "_job_intervals", None)
    check("已发布周期表", isinstance(pub, dict) and bool(pub), str(pub))
    # 下面的取值必须自己兜住 None：发布逻辑被改坏时 pub 就是 None，裸调 .get 会抛
    # AttributeError 把脚本 abort 在半路 —— 首条 FAIL 虽已记下，但汇总行丢失，
    # red-green 探针只能读成 -1/-1，看不出是红是绿。断言必须跑到底并打出汇总。
    view = pub if isinstance(pub, dict) else {}
    for name in INTRADAY_JOBS:
        want = next(x.seconds for x in specs if x.name == name)
        check(f"{name} 的发布周期 = spec 的真实周期", view.get(name) == want,
              f"发布 {view.get(name)} / spec {want}")
    check("cron 型不进周期表（它们该用 cron 预算，不能按 interval 算）",
          "tail_pick_select" not in view, str(sorted(view)))

    # _build_specs 末尾自动发布：首次启动与 reload 两条路径都经它，不会漏发。
    s2 = TradingScheduler.__new__(TradingScheduler)
    s2.settings, s2.jobs_cfg = _settings(), {}
    ctx2 = SimpleNamespace()
    s2.runner = SimpleNamespace(ctx=ctx2)
    s2._build_specs()                                       # noqa: SLF001
    check("_build_specs 自动发布（首次启动与 reload 都覆盖）",
          bool(getattr(ctx2, "_job_intervals", None)),
          str(getattr(ctx2, "_job_intervals", None)))

    # 裸构造（测试里常见，没有 runner/ctx）不能炸 —— 发布是尽力而为的旁路。
    s3 = TradingScheduler.__new__(TradingScheduler)
    s3.runner = None
    try:
        s3._publish_intervals(specs)                        # noqa: SLF001
        check("没有 ctx 时静默跳过（裸构造不炸）", True)
    except Exception as exc:                                # noqa: BLE001
        check("没有 ctx 时静默跳过（裸构造不炸）", False, f"{type(exc).__name__}: {exc}")


# ====================================================== [5] 让路告警限流
def test_lock_yield_warn_throttle() -> None:
    logger.info("\n[5] 让路告警限流（长任务持锁数分钟时不能把'没抢到锁'刷成屏）")
    saved_interval = J._LOCK_WARN_INTERVAL
    check("限流窗口为正", saved_interval > 0, f"{saved_interval}s")
    budget = 18.0
    with _captured("qmt_trade.scheduler") as cap:
        try:
            J._lock_warn_state.clear()
            J._warn_lock_yield("intraday", budget)
            J._warn_lock_yield("intraday", budget)
            J._warn_lock_yield("intraday", budget)
            warns = cap.messages()
            check("同一任务连续让路 3 次只打 1 条", len(warns) == 1, str(len(warns)))
            check("告警带任务名", bool(warns) and "intraday" in warns[0],
                  warns[0][:160] if warns else "")
            check("告警带本轮等待上限（按周期推导出来的那个数）",
                  bool(warns) and f"{budget:.0f}s" in warns[0],
                  warns[0][:160] if warns else "")

            J._LOCK_WARN_INTERVAL = 0.0        # 窗口归零 = 立刻放行，并带抑制计数
            J._warn_lock_yield("intraday", budget)
            warns = cap.messages()
            check("窗口过期后放行", len(warns) == 2, str(len(warns)))
            check("补上被抑制的次数", bool(warns) and "另有 2 次" in warns[-1],
                  warns[-1][:160] if warns else "")

            cap.records.clear()
            J._lock_warn_state.clear()
            J._warn_lock_yield("etf_t0_intraday", budget)
            J._warn_lock_yield("stock_t0_intraday", budget)
            warns = cap.messages()
            check("不同任务各自限流（不会互相吞掉）", len(warns) == 2, str(len(warns)))
        finally:
            J._LOCK_WARN_INTERVAL = saved_interval
            J._lock_warn_state.clear()


# ====================================================== [6] 第三方 logger 归并
def test_third_party_logger_merged() -> None:
    logger.info("\n[6] apscheduler 的告警走项目 formatter（不再裸打 stderr）")
    check("apscheduler 在归并名单里", "apscheduler" in THIRD_PARTY_LOGGERS,
          str(THIRD_PARTY_LOGGERS))
    aps = logging.getLogger("apscheduler")
    app = logging.getLogger("qmt_trade")
    msg = ('Execution of job "stock_t0_intraday (trigger: interval[0:00:30], '
           'next run at: 2026-09-16 00:06:01 CST)" skipped: '
           'maximum number of running instances reached (1)')
    noise = "APScheduler 内部噪音 INFO"

    # 复现生产条件：**真正的根 logger 一个 handler 都没有** —— 这正是当初
    # apscheduler 的 WARNING 只能经 logging.lastResort 裸打 stderr 的原因。
    root = logging.getLogger()
    saved_root = list(root.handlers)
    for h in saved_root:
        root.removeHandler(h)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            # StreamHandler 绑的是调用当下的 sys.stdout，所以必须在重定向内初始化
            setup_logging(console=True, force=True)
            aps.warning(msg)
            aps.info(noise)
    finally:
        for h in saved_root:
            root.addHandler(h)
        setup_logging(console=True, force=True)   # 把 handler 重新绑回真实 stdout

    text = out.getvalue()
    line = next((ln for ln in text.splitlines() if "maximum number" in ln), "")
    check("告警落到项目 stdout handler", "maximum number" in text)
    check("告警带项目格式（时间戳 | 级别 | trace | 来源）",
          bool(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \| WARNING "
                        r"\| \S+ \| apscheduler \| ", line)), line[:160])
    check("不再经 lastResort 裸打 stderr", "maximum number" not in err.getvalue(),
          err.getvalue()[:160])
    check("不再向根 logger 传播（避免重复输出）", aps.propagate is False)
    check("第三方 INFO 被压掉（只留告警）",
          noise not in text and aps.level == logging.WARNING, str(aps.level))
    check("复用同一批 handler（与 qmt_trade 日志格式一致）",
          bool(aps.handlers)
          and all(any(h is x for x in app.handlers) for h in aps.handlers),
          str([type(h).__name__ for h in aps.handlers]))
    check("格式串仍是项目统一格式", LOG_FORMAT.startswith("%(asctime)s"))


def main() -> int:
    test_in_window()
    test_production_specs_have_window()
    test_guarded_fire_window_gate()
    test_apscheduler_job_name()
    test_lock_budget_derivation()
    test_boundary_lock_bounded_wait()
    test_no_starvation_under_lock_hold()
    test_publish_intervals()
    test_lock_yield_warn_throttle()
    test_third_party_logger_merged()
    print(f"\n===== PASS={PASS} FAIL={FAIL} =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
