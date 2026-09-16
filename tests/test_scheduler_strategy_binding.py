"""调度任务 ↔ 策略启用状态「硬联动」回归测试。

为什么这套联动值得单独立一份测试：它失效的方式**不报错**——

- 策略关了、任务照跑：只是白烧 CPU（interval 任务交易日 660 次/天），日志里
  一片 ``[SKIP]``，没人会觉得是 bug；
- 任务停了、心跳没注销：24h 后体检把残留的 ``hb:job:*`` 判成「组件失联」→
  ERROR（blocking）→ 自动降级 REDUCE_ONLY，把"关掉一个策略"放大成"全系统禁止
  开仓"，现场看起来像风控发疯，根因却在一层之外。

两种都是静默劣化，只能靠断言锁住。覆盖：

1. 绑定表自身合法性（任务名/策略 id 写错会静默失效，比不写更糟）；
2. ``bound_strategy_enabled`` 判定语义；
3. ``_build_specs`` 打标：specs 含**全部**任务，但只有启用的算在日程上；
4. APScheduler 注册集合 == 启用集合（空跑的真正来源）；
5. ``reload()`` 的 diff 语义：关→开→关往返不抛 ``JobLookupError``；
6. 心跳不残留：``_beat_all`` / ``_sync_beats`` / ``JobRunner._beat`` 三处；
7. ``HealthMonitor.forget`` 与 ``heartbeat`` 严格互逆，且注销后不再误判失联；
8. 路由联动：``PUT /strategylab/{sid}/enabled`` → reload → ``GET /scheduler/jobs``；
9. 人工开关判定语义（缺省为开、读不出来回落为开、与策略门禁的 AND 真值表）；
10. 人工暂停下的 specs 打标与三态（``running``/``paused``/``disabled``）；
11. ``reload()`` 对人工开关的 diff 语义：暂停即摘除、开回即挂回、**不随策略复活**；
12. 人工暂停同样不留心跳（``_beat_all`` / ``JobRunner._beat`` 两处门禁）；
13. ``PUT /scheduler/job``：只写 ``<name>_enabled``、不碰 ``strategies.*``，
    以及四个曾被白名单漏掉的 interval/strategylab 任务改时刻不再 400。

用法::

    python tests/test_scheduler_strategy_binding.py

不写盘、不联网、不启动真实调度线程（APScheduler 以 ``paused`` 态验证 job 集合，
该状态下 job 已进 jobstore、增删改与生产完全一致，但永不触发）。
"""

from __future__ import annotations

import contextlib
import logging
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

# Windows 控制台默认 GBK，中文断言名会乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt_trade.core.config import Settings                        # noqa: E402
from qmt_trade.core.strategies import STANDALONE_STRATEGIES       # noqa: E402
from qmt_trade.scheduler import (                                 # noqa: E402
    JOB_MAP, JOB_STRATEGY_BINDING, JobRunner, TradingScheduler, bound_strategy_enabled,
    job_enabled, manual_enabled,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

PASS = FAIL = 0
D = date(2026, 8, 7)          # 周五，交易日

#: 受策略开关管辖的任务（= 绑定表的键），其余都是主管线任务、恒启用
GATED = tuple(JOB_STRATEGY_BINDING)
#: 主管线任务：系统骨架，不随任何单个策略起停
MAINLINE = ("data_sync", "regime", "selection", "research", "plan",
            "auction_check", "intraday", "reconcile", "review", "evolve")


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        logger.info("  [OK]   %s %s", name, extra)
    else:
        FAIL += 1
        logger.info("  [FAIL] %s %s", name, extra)
    return bool(cond)


# ------------------------------------------------------------------ 脚手架
def _settings(**enabled) -> Settings:
    """最小可用配置：只给 scheduler 段（决定时刻）与 strategies 段（决定启停）。

    ``env_overlay=False``：``QMT_*__*`` 环境变量不许干扰断言，否则本机跑了什么
    导出就会让测试结果漂移。
    """
    return Settings({
        "scheduler": {
            "enabled": True,
            "timezone": "Asia/Shanghai",
            "misfire_grace_seconds": 900,
            "jobs": {},
        },
        "strategies": {sid: {"enabled": bool(v)} for sid, v in enabled.items()},
    }, env_overlay=False)


@contextlib.contextmanager
def _patched_settings(settings: Settings):
    """把全局 ``get_settings()`` 换成测试配置。

    ``TradingScheduler.reload()`` 与 ``JobRunner._beat()`` 都是**函数内惰性导入**
    ``from ..core.config import get_settings``，所以改模块属性即可生效，无需触碰
    lru_cache。
    """
    import qmt_trade.core.config as cfg
    orig = cfg.get_settings
    cfg.get_settings = lambda: settings
    try:
        yield
    finally:
        cfg.get_settings = orig


def _tmp_db(tag: str) -> str:
    return str(Path(tempfile.mkdtemp(prefix=f"qmt_bind_{tag}_")) / "trade.db")


def _ctx(tag: str, settings: Settings | None = None):
    """全模拟上下文：MockProvider + 内存通知渠道，不联网、不碰真实账本。

    **settings 必须显式传**：``build_context`` 缺省会回落 ``get_settings()``，而它会去
    读 DuckDB 里已发布的配置（``data/db/qmt.duckdb``）—— 后端在跑时那个库被独占，
    当场 PermissionError；即便没被占用，断言也会跟着本机真实策略开关漂移。
    """
    from qmt_trade.app import build_context
    from qmt_trade.datahub.providers.mock import MockProvider
    from qmt_trade.ops.notify import MemoryChannel, Notifier
    return build_context("paper", settings=settings or _settings(),
                         db_path=_tmp_db(tag), initial_cash=1_000_000,
                         providers=[MockProvider(n_symbols=5)],
                         notifier=Notifier(channels=[MemoryChannel()]))


def _hb(ctx, name: str):
    """读任务心跳的库记录（``job:*`` 走跨 mode 共享库，与 heartbeat 写入同侧）。"""
    return ctx.shared_repos.system.get(f"hb:job:{name}")


def _job_ids(sched: TradingScheduler) -> set[str]:
    """APScheduler 里实际挂着的 job id 集合 —— 「会不会空跑」的唯一真相。"""
    return {j.id for j in sched._sched.get_jobs()}                  # noqa: SLF001


def _next_run(sched: TradingScheduler, name: str):
    """取 job 的下次执行时间；没挂上日程时返回 None（不要抛 AttributeError）。"""
    job = sched._sched.get_job(name)                                # noqa: SLF001
    return job.next_run_time if job is not None else None


@contextlib.contextmanager
def _paused_apscheduler(sched: TradingScheduler):
    """建好 APScheduler 并以 paused 态启动，退出时关掉。

    paused 态下 job 已落 jobstore（add/remove/reschedule 与生产一致），但调度线程
    没起、永不触发，所以可以放心断言 job 集合而不用担心真跑一遍任务。
    """
    sched._build_apscheduler()                                     # noqa: SLF001
    sched._sched.start(paused=True)                                # noqa: SLF001
    try:
        yield sched._sched                                         # noqa: SLF001
    finally:
        try:
            sched._sched.shutdown(wait=False)                      # noqa: SLF001
        except Exception:                                          # noqa: BLE001
            pass


# ====================================================== 1. 绑定表合法性
def test_binding_table() -> None:
    logger.info("\n[1] 绑定表自身合法性")
    check("绑定表非空", bool(JOB_STRATEGY_BINDING), f"{len(JOB_STRATEGY_BINDING)} 条")
    bad_jobs = [n for n in GATED if n not in JOB_MAP]
    check("任务名全部存在于 JOB_MAP（写错会静默失效）", not bad_jobs, str(bad_jobs))
    bad_sids = sorted({s for sids in JOB_STRATEGY_BINDING.values() for s in sids}
                      - set(STANDALONE_STRATEGIES))
    check("策略 id 全部是独立策略（写错会静默失效）", not bad_sids, str(bad_sids))
    check("每条绑定至少一个策略（空元组等于恒启用，属写错）",
          all(sids for sids in JOB_STRATEGY_BINDING.values()))
    overlap = [n for n in MAINLINE if n in JOB_STRATEGY_BINDING]
    check("主管线任务不受策略开关管辖", not overlap, str(overlap))
    # 用户点名的 5 个空跑大户必须在管辖范围内
    for must in ("stock_t0_intraday", "tail_pick_select", "tail_pick_exit",
                 "strategylab_open", "strategylab_run"):
        check(f"{must} 已登记绑定", must in JOB_STRATEGY_BINDING,
              str(JOB_STRATEGY_BINDING.get(must)))


# ====================================================== 2. 判定语义
def test_bound_strategy_enabled() -> None:
    logger.info("\n[2] bound_strategy_enabled 判定语义")
    check("无绑定任务恒启用（settings 全关也启用）",
          bound_strategy_enabled("data_sync", _settings()))
    check("settings=None 容错为启用（宁可多跑不可漏跑）",
          bound_strategy_enabled("tail_pick_select", None))
    check("绑定策略未配置 → 停用",
          not bound_strategy_enabled("tail_pick_select", _settings()))
    check("绑定策略显式 false → 停用",
          not bound_strategy_enabled("tail_pick_select", _settings(tail_pick=False)))
    check("绑定策略 true → 启用",
          bound_strategy_enabled("tail_pick_select", _settings(tail_pick=True)))
    # 多策略绑定是「任一启用即启用」，不是「全部启用」
    check("多绑定：只开 dip_buy 也启用 strategylab_run",
          bound_strategy_enabled("strategylab_run", _settings(dip_buy=True)))
    check("多绑定：只开 dip_buy 不启用 strategylab_open（未绑定 dip_buy）",
          not bound_strategy_enabled("strategylab_open", _settings(dip_buy=True)))
    check("多绑定：开 second_board 同时启用 open 与 run",
          bound_strategy_enabled("strategylab_open", _settings(second_board=True))
          and bound_strategy_enabled("strategylab_run", _settings(second_board=True)))
    # 脏配置不许把调度器整个带崩
    dirty = Settings({"strategies": {"tail_pick": {"enabled": "yes-please"}}},
                     env_overlay=False)
    check("非布尔真值按启用处理（bool('yes-please')）",
          bound_strategy_enabled("tail_pick_select", dirty))


# ====================================================== 3. specs 打标
def test_specs_gated() -> None:
    logger.info("\n[3] _build_specs 打标：全量 specs + 启用标记")
    settings = _settings(etf_t0=True)          # 只开 ETF T+0
    runner = JobRunner(_ctx("specs", settings), trade_date=D)
    try:
        sched = TradingScheduler(runner, settings)
        names = [s.name for s in sched.specs]
        check("specs 含全部任务（UI 要展示停用的是哪些）",
              set(MAINLINE) | set(GATED) <= set(names), str(len(names)))
        by = {s.name: s for s in sched.specs}
        check("etf_t0 已开 → etf_t0_intraday 启用", by["etf_t0_intraday"].enabled)
        off = sorted(n for n, s in by.items() if not s.enabled)
        check("其余策略任务全部停用",
              off == sorted(set(GATED) - {"etf_t0_intraday"}), str(off))
        check("主管线任务全部启用", all(by[n].enabled for n in MAINLINE))
        check("bound_strategies 与绑定表一致",
              all(by[n].bound_strategies == JOB_STRATEGY_BINDING.get(n, ())
                  for n in names))
        check("status: 启用 → running", by["data_sync"].status == "running")
        check("status: 停用 → disabled", by["tail_pick_select"].status == "disabled")
        check("status_detail 点明是哪些策略没开",
              "tail_pick" in by["tail_pick_select"].status_detail,
              by["tail_pick_select"].status_detail)
        check("启用任务 status_detail 为空（UI 不挂多余 tooltip）",
              by["data_sync"].status_detail == "")
        desc = sched.describe()
        check("describe() 标注停用项", "[停用]" in desc)
        check("describe() 未误标启用项",
              "data_sync" in desc and "data_sync" not in
              [ln.split()[0] for ln in desc.splitlines() if "[停用]" in ln])
    finally:
        runner.ctx.close()


# ====================================================== 4. 注册集合
def test_apscheduler_registration() -> None:
    logger.info("\n[4] APScheduler 注册集合 == 启用集合（空跑的真正来源）")
    settings = _settings(etf_t0=True)
    runner = JobRunner(_ctx("reg", settings), trade_date=D)
    try:
        sched = TradingScheduler(runner, settings)
        with _paused_apscheduler(sched):
            ids = _job_ids(sched)
            want = {s.name for s in sched.specs if s.enabled}
            check("注册的 job 与启用集合完全一致", ids == want,
                  f"多注册={sorted(ids - want)} 漏注册={sorted(want - ids)}")
            check("停用任务一个都没挂上日程", not (ids & set(GATED) - {"etf_t0_intraday"}),
                  str(sorted(ids & set(GATED))))
            check("etf_t0_intraday 已挂上", "etf_t0_intraday" in ids)
            check("主管线任务全部挂上", set(MAINLINE) <= ids)
    finally:
        runner.ctx.close()


# ====================================================== 5. reload diff
def test_reload_diff() -> None:
    logger.info("\n[5] reload() 增删改三管齐下")
    settings = _settings(tail_pick=False)
    runner = JobRunner(_ctx("reload", settings), trade_date=D)
    try:
        sched = TradingScheduler(runner, settings)
        with _paused_apscheduler(sched), _patched_settings(settings):
            check("初始：tail_pick 两条计划未注册",
                  not ({"tail_pick_select", "tail_pick_exit"} & _job_ids(sched)))

            settings.set("strategies.tail_pick.enabled", True)
            ok = sched.reload()
            ids = _job_ids(sched)
            # 旧实现只会 reschedule，而 job 根本不存在 → JobLookupError → reload 失败
            check("重新启用后 reload 返回 True（未抛 JobLookupError）", ok is True)
            check("重新启用后两条计划挂回日程",
                  {"tail_pick_select", "tail_pick_exit"} <= ids, str(sorted(ids)))

            before = _next_run(sched, "tail_pick_select")
            settings.set("scheduler.jobs.tail_pick_select", "14:55")
            check("改时刻后 reload 仍成功", sched.reload() is True)
            after = _next_run(sched, "tail_pick_select")
            # 两端都非 None 才算「时刻真的被改掉了」：job 压根没挂上时 before/after
            # 都是 None，光比 != 会让「忘记 add_job」这种缺陷蒙混过关。
            check("改时刻走 reschedule 分支（下次执行时间已变）",
                  before is not None and after is not None and before != after,
                  f"{before} → {after}")

            settings.set("strategies.tail_pick.enabled", False)
            check("再次停用后 reload 仍成功", sched.reload() is True)
            ids = _job_ids(sched)
            check("再次停用后从日程摘除",
                  not ({"tail_pick_select", "tail_pick_exit"} & ids))
            check("停用不牵连主管线任务", set(MAINLINE) <= ids)
            check("reload 后 specs 与 job 集合保持一致",
                  {s.name for s in sched.specs if s.enabled} == ids)

            # 幂等：重复 reload 不该把 job 弄丢或重复
            sched.reload()
            check("重复 reload 幂等", _job_ids(sched) == ids)
    finally:
        runner.ctx.close()


# ====================================================== 6. 心跳不残留
def test_heartbeat_not_residual() -> None:
    logger.info("\n[6] 停用任务不留心跳（防体检误判失联 → 误拉闸）")
    settings = _settings(etf_t0=True, tail_pick=False)
    ctx = _ctx("beat", settings)
    try:
        runner = JobRunner(ctx, trade_date=D)
        sched = TradingScheduler(runner, settings)
        with _patched_settings(settings):
            # 伪造"上一进程还在跑 tail_pick 时留下的"心跳
            ctx.monitor.heartbeat("job:tail_pick_select")
            check("前置：残留心跳已入库", _hb(ctx, "tail_pick_select") is not None)

            sched._beat_all()                                      # noqa: SLF001
            check("启用任务补到心跳", _hb(ctx, "etf_t0_intraday") is not None)
            check("停用任务的残留心跳被注销", _hb(ctx, "tail_pick_select") is None)
            check("停用任务也不在内存心跳表",
                  "job:tail_pick_select" not in ctx.monitor._beats)  # noqa: SLF001

            # 旁路：人工点「立即运行」/ CLI run --once 单跑了个停用任务
            runner._beat("tail_pick_select")                       # noqa: SLF001
            check("JobRunner._beat 对停用任务不写心跳",
                  _hb(ctx, "tail_pick_select") is None)
            runner._beat("etf_t0_intraday")                        # noqa: SLF001
            check("JobRunner._beat 对启用任务照常写心跳",
                  _hb(ctx, "etf_t0_intraday") is not None)

            # 重新启用后必须能恢复打心跳，否则会被判失联
            settings.set("strategies.tail_pick.enabled", True)
            sched.reload()
            runner._beat("tail_pick_select")                       # noqa: SLF001
            check("策略重新启用后恢复打心跳",
                  _hb(ctx, "tail_pick_select") is not None)
    finally:
        ctx.close()


# ====================================================== 7. forget 互逆
def test_monitor_forget() -> None:
    logger.info("\n[7] HealthMonitor.forget 与 heartbeat 互逆")
    ctx = _ctx("forget")
    try:
        mon = ctx.monitor
        mon.heartbeat("job:probe_forget")
        check("heartbeat 写入库", _hb(ctx, "probe_forget") is not None)
        mon.forget("job:probe_forget")
        check("forget 删除库记录", _hb(ctx, "probe_forget") is None)
        check("forget 删除内存记录", "job:probe_forget" not in mon._beats)  # noqa: SLF001
        mon.forget("job:never_existed")
        check("forget 不存在的组件不抛异常（幂等）", True)

        # 关键回归：注销后体检不许再把它算作"在编却失联"的组件
        mon.heartbeat("job:stale_probe")
        mon._beats["job:stale_probe"] = time.time() - 200_000      # noqa: SLF001
        res = mon._check_heartbeats()                              # noqa: SLF001
        check("前置：过期心跳被判失联", not res.ok, res.message)
        mon.forget("job:stale_probe")
        res = mon._check_heartbeats()                              # noqa: SLF001
        check("forget 后不再判失联（不会误降级 REDUCE_ONLY）",
              "stale_probe" not in res.message, res.message)
        check("forget 未误伤其它组件的心跳",
              "probe_forget" not in mon._read_db_beats())          # noqa: SLF001
    finally:
        ctx.close()


# ====================================================== 8. 路由联动
def test_route_wiring() -> None:
    logger.info("\n[8] 路由联动：改策略开关 → reload → /scheduler/jobs")
    from fastapi import HTTPException

    import qmt_trade.core.config as cfg_mod
    import server.routers.overview as ov_mod
    import server.routers.strategylab as sl_mod

    settings = _settings(tail_pick=False)
    ctx = _ctx("route", settings)
    saved: list = []
    orig = (cfg_mod.get_settings, ov_mod._ctx,                 # noqa: SLF001
            sl_mod.load_settings_editor, sl_mod.save_settings)
    # 打桩落盘：绝不改用户真实的 settings.yaml / strategies/*.yaml
    cfg_mod.get_settings = lambda: settings
    ov_mod._ctx = lambda mode="paper": ctx                     # noqa: SLF001
    sl_mod.load_settings_editor = lambda: settings
    sl_mod.save_settings = lambda s: saved.append(s)
    try:
        runner = JobRunner(ctx, trade_date=D)
        sched = TradingScheduler(runner, settings)
        req = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(scheduler=sched)))

        jobs = {j["name"]: j for j in ov_mod.scheduler_jobs(mode="paper")["jobs"]}
        check("全部任务都回传（含停用的）",
              set(MAINLINE) | set(GATED) <= set(jobs), str(len(jobs)))
        j = jobs["tail_pick_select"]
        check("停用时 status=disabled", j["status"] == "disabled")
        check("停用时 enabled=false", j["enabled"] is False)
        check("停用时 next_run=null（不给用户假期待）", j["next_run"] is None)
        check("停用时 status_detail 点明原因", "tail_pick" in j["status_detail"],
              j["status_detail"])
        check("bound_strategies 回传给前端拼 tooltip",
              j["bound_strategies"] == ["tail_pick"], str(j["bound_strategies"]))
        check("主管线任务 status=running 且有 next_run",
              jobs["data_sync"]["status"] == "running" and bool(jobs["data_sync"]["next_run"]))
        check("schedule_text 标注停用", "[停用]" in jobs["data_sync"].get("label", "")
              or "[停用]" in ov_mod.scheduler_jobs(mode="paper")["schedule_text"])

        r = sl_mod.set_enabled("tail_pick", sl_mod.StrategyEnableIn(enabled=True), req)
        check("PUT enabled 返回 ok", r["ok"] is True and r["enabled"] is True)
        check("配置已交给 save_settings 落盘", len(saved) == 1)
        check("常驻调度器已热更新（reloaded=True）", r["reloaded"] is True)
        check("affected_jobs 告知前端哪些任务随之起停",
              r["affected_jobs"] == ["tail_pick_exit", "tail_pick_select"],
              str(r["affected_jobs"]))

        jobs = {j2["name"]: j2 for j2 in ov_mod.scheduler_jobs(mode="paper")["jobs"]}
        check("开启后 status=running", jobs["tail_pick_select"]["status"] == "running")
        check("开启后 next_run 非空", bool(jobs["tail_pick_select"]["next_run"]))
        check("常驻调度器 specs 同步翻转",
              {s.name for s in sched.specs if not s.enabled}
              == set(GATED) - {"tail_pick_select", "tail_pick_exit"})

        # 关掉后必须立刻从日程摘除，这才是"不空跑"的落点
        sl_mod.set_enabled("tail_pick", sl_mod.StrategyEnableIn(enabled=False), req)
        jobs = {j2["name"]: j2 for j2 in ov_mod.scheduler_jobs(mode="paper")["jobs"]}
        check("关闭后立刻回到 disabled", jobs["tail_pick_select"]["status"] == "disabled")

        try:
            sl_mod.set_enabled("not_a_strategy",
                               sl_mod.StrategyEnableIn(enabled=True), req)
            check("未知策略被拒（400）", False)
        except HTTPException as exc:
            check("未知策略被拒（400）", exc.status_code == 400, str(exc.detail))

        # 拿不到常驻调度器（如 CLI 起的后端）时不许 500，只提示重启
        r2 = sl_mod.set_enabled("tail_pick", sl_mod.StrategyEnableIn(enabled=True),
                                SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
        check("无常驻调度器时 reloaded=False 且提示重启",
              r2["reloaded"] is False and "重启" in r2["hint"], r2["hint"])
    finally:
        (cfg_mod.get_settings, ov_mod._ctx,                    # noqa: SLF001
         sl_mod.load_settings_editor, sl_mod.save_settings) = orig
        ctx.close()


# ====================================================== 9. 人工开关判定语义
def test_manual_gate_semantics() -> None:
    logger.info("\n[9] manual_enabled / job_enabled 判定语义（人工开关 AND 策略门禁）")

    class _Boom:
        """读配置就炸的 settings —— 用来验证 except 分支的回落方向。"""

        def get(self, *a, **k):
            raise RuntimeError("配置读不出来")

    check("缺省未配置 → 人工开关为开（不许凭空缺键把任务停用）",
          manual_enabled("data_sync", _settings()))
    check("settings=None → 人工开关容错为开", manual_enabled("data_sync", None))
    check("settings.get 抛异常 → 回落为开（漏跑比多跑更容易被发现）",
          manual_enabled("data_sync", _Boom()))

    off = _settings()
    off.set("scheduler.jobs.data_sync_enabled", False)
    check("显式 false → 人工关", not manual_enabled("data_sync", off))
    on = _settings()
    on.set("scheduler.jobs.data_sync_enabled", True)
    check("显式 true → 人工开", manual_enabled("data_sync", on))

    # AND 真值表：四种组合逐一验，缺一种就可能把「策略关」误当「人工关」
    def _triple(manual: bool, strategy: bool) -> tuple[bool, bool, bool]:
        s = _settings(tail_pick=strategy)
        s.set("scheduler.jobs.tail_pick_select_enabled", manual)
        return (manual_enabled("tail_pick_select", s),
                bound_strategy_enabled("tail_pick_select", s),
                job_enabled("tail_pick_select", s))

    for manual, strategy, want in ((True, True, True), (True, False, False),
                                   (False, True, False), (False, False, False)):
        m, g, got = _triple(manual, strategy)
        check(f"人工={manual} 策略={strategy} → enabled={want}",
              (m, g, got) == (manual, strategy, want), f"实测 {(m, g, got)}")

    check("两道闸门各自独立可读（UI 要分开回显）",
          _triple(False, True)[:2] == (False, True))
    # 人工开关对主管线任务同样有效：没有策略绑定不等于"关不掉"
    check("主管线任务也能人工暂停（无绑定 → 只看人工开关）",
          not job_enabled("data_sync", off) and not manual_enabled("data_sync", off))


# ====================================================== 10. 人工暂停下的打标与三态
def test_manual_gate_specs() -> None:
    logger.info("\n[10] _apply_gates 打标 + 三态 status + 注册集合（人工暂停）")
    settings = _settings(tail_pick=True)          # 策略开着，只关人工开关
    settings.set("scheduler.jobs.tail_pick_select_enabled", False)
    settings.set("scheduler.jobs.data_sync_enabled", False)
    runner = JobRunner(_ctx("manual_specs", settings), trade_date=D)
    try:
        sched = TradingScheduler(runner, settings)
        by = {s.name: s for s in sched.specs}
        check("人工关 + 策略开 → strategy_gate 仍为 True（两道闸门分开存）",
              by["tail_pick_select"].strategy_gate is True)
        check("人工关 + 策略开 → manual_enabled=False",
              by["tail_pick_select"].manual_enabled is False)
        check("人工关 + 策略开 → enabled=False（AND 语义）",
              by["tail_pick_select"].enabled is False)
        check("三态：人工关 → status=paused（不是 disabled）",
              by["tail_pick_select"].status == "paused", by["tail_pick_select"].status)
        check("paused 的 status_detail 点明是手动暂停（UI tooltip 据此指路）",
              "手动暂停" in by["tail_pick_select"].status_detail,
              by["tail_pick_select"].status_detail)
        check("主管线任务同样进入 paused",
              by["data_sync"].status == "paused" and by["data_sync"].enabled is False)
        check("未暂停的任务仍是 running", by["tail_pick_exit"].status == "running")
        check("状态码只可能是三态之一（新增值必须同步 labels.ts）",
              all(s.status in ("running", "paused", "disabled") for s in sched.specs))

        with _paused_apscheduler(sched):
            ids = _job_ids(sched)
            check("人工暂停的任务一个都没挂上日程（不空跑）",
                  not ({"tail_pick_select", "data_sync"} & ids),
                  f"误挂={sorted({'tail_pick_select', 'data_sync'} & ids)}")
            check("注册集合 == 启用集合", ids == {s.name for s in sched.specs if s.enabled})
            check("只摘人工暂停的那个，同策略的兄弟任务照跑", "tail_pick_exit" in ids)
            check("paused 任务没有 next_run（给用户假期待比不给更糟）",
                  _next_run(sched, "tail_pick_select") is None)
    finally:
        runner.ctx.close()


# ====================================================== 11. 人工开关的 reload diff
def test_manual_gate_reload_diff() -> None:
    logger.info("\n[11] reload() 对人工开关的 diff：摘除 / 挂回 / 不随策略复活")
    settings = _settings(tail_pick=True)
    runner = JobRunner(_ctx("manual_reload", settings), trade_date=D)
    try:
        sched = TradingScheduler(runner, settings)
        with _paused_apscheduler(sched), _patched_settings(settings):
            check("前置：tail_pick 两条计划都挂着",
                  {"tail_pick_select", "tail_pick_exit"} <= _job_ids(sched))

            settings.set("scheduler.jobs.tail_pick_select_enabled", False)
            # 旧实现只会 reschedule，而 job 已被摘掉 → JobLookupError → reload 整体失败
            check("人工暂停后 reload 返回 True（未抛 JobLookupError）",
                  sched.reload() is True)
            ids = _job_ids(sched)
            check("人工暂停的任务从日程摘除", "tail_pick_select" not in ids)
            check("不牵连同策略的兄弟任务", "tail_pick_exit" in ids)
            check("describe() 标注人工暂停", "[人工暂停]" in sched.describe())

            # 两道闸门都关：status 仍显示 paused（人工开关在用户手边，可操作性优先），
            # 但策略那条原因必须一并写进 detail，否则用户开了开关发现还是灰的会以为没保存上
            settings.set("strategies.tail_pick.enabled", False)
            check("两道闸门都关后 reload 成功", sched.reload() is True)
            by = {s.name: s for s in sched.specs}
            check("两道都关 → status=paused（不是 disabled）",
                  by["tail_pick_select"].status == "paused", by["tail_pick_select"].status)
            check("两道都关 → status_detail 两条原因都在",
                  "手动暂停" in by["tail_pick_select"].status_detail
                  and "tail_pick" in by["tail_pick_select"].status_detail,
                  by["tail_pick_select"].status_detail)

            # 用户直觉：手动关掉的任务，绑定策略反复开关也不该让它自己复活
            settings.set("strategies.tail_pick.enabled", True)
            check("策略重新启用后 reload 成功", sched.reload() is True)
            check("策略开回来也不复活人工暂停的任务",
                  "tail_pick_select" not in _job_ids(sched))
            check("复活与否只看人工开关（enabled 仍为 False）",
                  {s.name: s for s in sched.specs}["tail_pick_select"].enabled is False)

            settings.set("scheduler.jobs.tail_pick_select_enabled", True)
            check("人工开回后 reload 成功", sched.reload() is True)
            check("人工开回后挂回日程", "tail_pick_select" in _job_ids(sched))
            after = {s.name: s for s in sched.specs}["tail_pick_select"]
            check("开回后 status=running 且 detail 清空",
                  after.status == "running" and after.status_detail == "")
            check("开回后有 next_run", _next_run(sched, "tail_pick_select") is not None)

            # 幂等：重复 reload 不该把 job 弄丢或重复
            ids = _job_ids(sched)
            sched.reload()
            check("重复 reload 幂等", _job_ids(sched) == ids)
    finally:
        runner.ctx.close()


# ====================================================== 12. 人工暂停不留心跳
def test_manual_pause_heartbeat() -> None:
    logger.info("\n[12] 人工暂停的任务不留心跳（防体检误判失联 → 误拉闸）")
    settings = _settings(etf_t0=True)
    settings.set("scheduler.jobs.etf_t0_intraday_enabled", False)
    ctx = _ctx("manual_beat", settings)
    try:
        runner = JobRunner(ctx, trade_date=D)
        sched = TradingScheduler(runner, settings)
        with _patched_settings(settings):
            # 伪造"上一进程还在跑 etf_t0 时留下的"心跳
            ctx.monitor.heartbeat("job:etf_t0_intraday")
            check("前置：残留心跳已入库", _hb(ctx, "etf_t0_intraday") is not None)

            sched._beat_all()                                      # noqa: SLF001
            check("人工暂停任务的残留心跳被注销", _hb(ctx, "etf_t0_intraday") is None)
            check("未暂停的任务照常补心跳", _hb(ctx, "data_sync") is not None)

            # 旁路：人工暂停时点「立即运行」补跑一次（这是有意保留的逃生口）
            runner._beat("etf_t0_intraday")                        # noqa: SLF001
            check("补跑不给暂停任务写回心跳（否则 24h 后误判失联拉闸）",
                  _hb(ctx, "etf_t0_intraday") is None)

            settings.set("scheduler.jobs.etf_t0_intraday_enabled", True)
            sched.reload()
            runner._beat("etf_t0_intraday")                        # noqa: SLF001
            check("人工开回后恢复打心跳（否则会被判失联）",
                  _hb(ctx, "etf_t0_intraday") is not None)
    finally:
        ctx.close()


# ====================================================== 13. 编辑接口的人工开关
def test_route_manual_switch() -> None:
    logger.info("\n[13] PUT /scheduler/job：人工开关落盘 + 任务名白名单单一来源")
    from fastapi import HTTPException

    import qmt_trade.core.config as cfg_mod
    import server.routers.overview as ov_mod

    settings = _settings(tail_pick=True)
    ctx = _ctx("manual_route", settings)
    saved: list = []
    orig = (cfg_mod.get_settings, ov_mod._ctx,                  # noqa: SLF001
            ov_mod.load_settings_editor, ov_mod.save_settings)
    # 打桩落盘：绝不改用户真实的 settings.yaml
    cfg_mod.get_settings = lambda: settings
    ov_mod._ctx = lambda mode="paper": ctx                      # noqa: SLF001
    ov_mod.load_settings_editor = lambda: settings
    ov_mod.save_settings = lambda s: saved.append(s)
    try:
        runner = JobRunner(ctx, trade_date=D)
        sched = TradingScheduler(runner, settings)
        req = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(scheduler=sched)))

        with _paused_apscheduler(sched):
            r = ov_mod.scheduler_update_job(
                ov_mod.JobScheduleIn(name="tail_pick_select", enabled=False), req)
            check("只改状态也接受（不要求同时带时刻）",
                  r["ok"] is True and r["changed"] == ["状态"], str(r["changed"]))
            check("落盘的是人工开关键 scheduler.jobs.<name>_enabled",
                  settings.get("scheduler.jobs.tail_pick_select_enabled") is False)
            check("没碰 strategies.*（策略门禁那半边归「策略实验室」管）",
                  settings.get("strategies.tail_pick.enabled") is True)
            check("已交给 save_settings 落盘", len(saved) == 1)
            check("回传两道闸门 + 最终生效状态",
                  (r["manual_enabled"], r["strategy_gate"], r["enabled"])
                  == (False, True, False), str(r))
            check("常驻调度器已热更新（reloaded=True）", r["reloaded"] is True)
            check("热更新后从日程摘除", "tail_pick_select" not in _job_ids(sched))

            jobs = {j["name"]: j for j in ov_mod.scheduler_jobs(mode="paper")["jobs"]}
            j = jobs["tail_pick_select"]
            check("GET 回传 status=paused（人工暂停 ≠ 策略停用）",
                  j["status"] == "paused", j["status"])
            check("GET 分开回传两道闸门供编辑弹窗回显",
                  j["manual_enabled"] is False and j["strategy_gate"] is True)
            check("GET 回传 status_detail 说明原因", "手动暂停" in j["status_detail"],
                  j["status_detail"])
            check("GET 回传 next_run=null", j["next_run"] is None)
            check("GET 未把 paused 误标为 disabled",
                  all(jj["status"] != "disabled" for jj in jobs.values()
                      if jj["strategy_gate"]), "策略开着的任务不该显示 disabled")

            r2 = ov_mod.scheduler_update_job(
                ov_mod.JobScheduleIn(name="tail_pick_select", enabled=True), req)
            check("开回后回传 enabled=True", r2["enabled"] is True)
            check("开回后挂回日程", "tail_pick_select" in _job_ids(sched))

            # 白名单单一来源回归：这四个任务此前一律 400「未知调度任务」
            probes = (("strategylab_open", {"time": "09:36"}),
                      ("strategylab_run", {"time": "14:46"}),
                      ("etf_t0_intraday", {"interval_seconds": 31}),
                      ("stock_t0_intraday", {"interval_seconds": 31}))
            for nm, kw in probes:
                try:
                    rr = ov_mod.scheduler_update_job(
                        ov_mod.JobScheduleIn(name=nm, **kw), req)
                    check(f"{nm} 可编辑执行计划（旧白名单漏掉 → 400）",
                          rr["ok"] is True, str(rr["changed"]))
                except HTTPException as exc:
                    check(f"{nm} 可编辑执行计划（旧白名单漏掉 → 400）", False,
                          f"{exc.status_code} {exc.detail}")
            check("改动真的落到 JOB_TIME_KEYS / JOB_WINDOW_KEYS 指的配置键",
                  settings.get("scheduler.jobs.strategylab_open") == "09:36"
                  and settings.get("scheduler.jobs.strategylab_run") == "14:46"
                  and settings.get("scheduler.jobs.etf_t0_interval_seconds") == 31,
                  str(settings.get("scheduler.jobs")))

            try:
                ov_mod.scheduler_update_job(
                    ov_mod.JobScheduleIn(name="not_a_job", enabled=True), req)
                check("未知任务被拒（400）", False)
            except HTTPException as exc:
                check("未知任务被拒（400）",
                      exc.status_code == 400 and "未知调度任务" in str(exc.detail),
                      str(exc.detail))
            try:
                ov_mod.scheduler_update_job(ov_mod.JobScheduleIn(name="data_sync"), req)
                check("时刻与状态都没传 → 400（不静默 no-op）", False)
            except HTTPException as exc:
                check("时刻与状态都没传 → 400（不静默 no-op）",
                      exc.status_code == 400, str(exc.detail))
            try:
                ov_mod.scheduler_update_job(
                    ov_mod.JobScheduleIn(name="plan", time="25:99"), req)
                check("非法 HH:MM → 400（绝不静默兜底）", False)
            except HTTPException as exc:
                check("非法 HH:MM → 400（绝不静默兜底）",
                      exc.status_code == 400, str(exc.detail))
            check("校验失败时不落盘（save_settings 未被多调）",
                  settings.get("scheduler.jobs.plan") in (None, "09:00"),
                  str(settings.get("scheduler.jobs.plan")))
    finally:
        (cfg_mod.get_settings, ov_mod._ctx,                  # noqa: SLF001
         ov_mod.load_settings_editor, ov_mod.save_settings) = orig
        ctx.close()


def main() -> int:
    for fn in (test_binding_table, test_bound_strategy_enabled, test_specs_gated,
               test_apscheduler_registration, test_reload_diff,
               test_heartbeat_not_residual, test_monitor_forget, test_route_wiring,
               test_manual_gate_semantics, test_manual_gate_specs,
               test_manual_gate_reload_diff, test_manual_pause_heartbeat,
               test_route_manual_switch):
        fn()
    logger.info("\n" + "=" * 60)
    logger.info("通过 %d 项，失败 %d 项", PASS, FAIL)
    if FAIL:
        logger.info("结论：策略↔任务联动存在缺口，停用策略会空跑或误拉闸")
        return 1
    logger.info("结论：绑定表、日程注册、热更新、心跳注销、路由联动、人工开关全部符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
