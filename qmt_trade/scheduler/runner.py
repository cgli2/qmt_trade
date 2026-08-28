"""APScheduler 封装：把 :class:`JobRunner` 的任务挂到时间表上。

两点刻意为之的设计：

1. **APScheduler 是可选依赖**。装了就用 ``BackgroundScheduler``；没装则退化为
   内置的轮询循环（精度秒级，够用）。调度器本身不该成为系统起不来的理由。
2. **盘中不是 cron 而是 interval**。09:30–15:00 每 N 秒巡检一次持仓，
   cron 表达不了"区间内高频"，用 interval + 时段判断更直白。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable

from .jobs import JobResult, JobRunner

logger = logging.getLogger(__name__)

#: 每个调度任务的中文用途说明（工作台展示用，让非技术用户也能看懂）
JOB_DESCRIPTIONS: dict[str, str] = {
    "data_sync": "预热当日行情/基本面数据并做质量体检，数据不合格会自动拉闸",
    "regime": "判定市场状态（趋势/震荡/风险规避），决定当日总仓位上限",
    "selection": "硬条件过滤 + 多因子打分排序，产出当日候选池",
    "research": "多智能体 LLM 深度研判候选股，产出交易意图与最终精选（耗时较长）",
    "plan": "把研判意图转成待执行交易计划，只落库不下单，便于盘前人工复核",
    "auction_check": "集合竞价复核：停牌、一字板、跳空过大的计划直接作废",
    "intraday": "盘中高频巡检：先做持仓止损/止盈守护，再执行待办计划",
    "reconcile": "盘后与券商对账，差异过大自动降级为只允许减仓",
    "review": "复盘归因 + 因子 IC 统计 + 生成日报",
    "evolve": "周度进化：策略池调权 + 周报 + 阶段分析报告",
    "tail_pick_select": "尾盘选股法：14:30 经 8 层筛选选股，paper/live 下买入、隔夜持有（独立短线）",
    "tail_pick_exit": "尾盘选股法：次日 09:30 开盘 30min 内离场（一夜持股纪律，独立短线）",
    "strategylab_open": "策略实验室：09:35 开盘买入相位（打板/二板，按 strategies.<sid>.enabled 启停）",
    "strategylab_run": "策略实验室：14:45 尾盘买入+持仓管理（低吸/趋势）+ 日收益入策略池",
    "etf_t0_intraday": "ETF T+0（底仓做T）：盘中每 N 秒巡检（按 strategies.etf_t0.enabled 启停，独立于主策略）",
    "stock_t0_intraday": "个股存量持仓做T（高抛低吸）：盘中每 N 秒巡检（按 strategies.stock_t0.enabled 启停，独立于主策略）",
}

_DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DOW_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _parse_hm(text: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = str(text).split(":")
        return int(h), int(m)
    except Exception:                                # noqa: BLE001
        logger.warning("时间格式非法 %r，改用 %02d:%02d", text, *default)
        return default


@dataclass
class JobSpec:
    """一条调度计划。``kind`` ∈ {cron, interval}。"""
    name: str
    kind: str
    hour: int = 0
    minute: int = 0
    seconds: int = 0
    day_of_week: str | None = None
    start_time: dtime | None = None
    end_time: dtime | None = None
    description: str = ""

    @property
    def time_label(self) -> str:
        """人类可读的执行计划（如「每日 06:30」「周日 10:00」），UI 直接展示。"""
        if self.kind == "cron":
            hm = f"{self.hour:02d}:{self.minute:02d}"
            if self.day_of_week:
                try:
                    return f"{_DOW_CN[_DOW.index(self.day_of_week)]} {hm}"
                except ValueError:
                    return f"{self.day_of_week} {hm}"
            return f"每日 {hm}"
        window = ""
        if self.start_time and self.end_time:
            window = f"（{self.start_time:%H:%M}–{self.end_time:%H:%M}）"
        return f"每 {self.seconds} 秒巡检{window}"

    def cron_expr(self) -> str:
        """标准 5 段 cron 表达式（分 时 日 月 周）。

        interval 型任务（盘中巡检）没有对应 cron，返回空串——
        它表达的是"窗口内每 N 秒"，cron 描述不了。
        """
        if self.kind != "cron":
            return ""
        dow = "*" if not self.day_of_week else self.day_of_week
        return f"{self.minute} {self.hour} * * {dow}"

    def describe(self) -> str:
        return f"{self.name:<14} {self.kind:<8} {self.time_label}"


class TradingScheduler:
    """交易日程调度器。

    ``start()`` 非阻塞；``run_forever()`` 阻塞直到 Ctrl-C。
    """

    def __init__(self, runner: JobRunner, settings=None):
        self.runner = runner
        self.settings = settings if settings is not None else runner.ctx.settings
        cfg = self.settings.section("scheduler") or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.timezone = str(cfg.get("timezone", "Asia/Shanghai"))
        self.misfire = int(cfg.get("misfire_grace_seconds", 900))
        self.jobs_cfg: dict[str, Any] = dict(cfg.get("jobs", {}) or {})
        self.specs: list[JobSpec] = self._build_specs()
        self._sched = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: 每个任务上一次触发的分钟标记，退化模式下用于去重
        self._fired: dict[str, str] = {}
        self.results: list[JobResult] = []

    # ------------------------------------------------------------ 计划表
    def _build_specs(self) -> list[JobSpec]:
        c = self.jobs_cfg

        def _cron_spec(name: str, cfg_key: str, default: str,
                       default_hm: tuple[int, int]) -> JobSpec:
            return JobSpec(name, "cron", *_parse_hm(c.get(cfg_key, default), default_hm),
                           description=JOB_DESCRIPTIONS.get(name, ""))

        specs = [
            _cron_spec("data_sync", "data_sync", "06:30", (6, 30)),
            _cron_spec("regime", "regime", "07:30", (7, 30)),
            _cron_spec("selection", "selection", "08:00", (8, 0)),
            _cron_spec("research", "llm_research", "08:15", (8, 15)),
            _cron_spec("plan", "plan", "09:00", (9, 0)),
            _cron_spec("auction_check", "auction_check", "09:20", (9, 20)),
            _cron_spec("reconcile", "reconcile", "15:05", (15, 5)),
            _cron_spec("review", "review", "16:00", (16, 0)),
        ]
        eh, em = _parse_hm(c.get("evolve", "10:00"), (10, 0))
        dow = int(c.get("evolve_weekday", 6))        # 0=周一 … 6=周日
        specs.append(JobSpec("evolve", "cron", eh, em,
                             day_of_week=_DOW[dow % 7],
                             description=JOB_DESCRIPTIONS.get("evolve", "")))

        sh, sm = _parse_hm(c.get("intraday_start", "09:30"), (9, 30))
        eh2, em2 = _parse_hm(c.get("intraday_end", "15:00"), (15, 0))
        specs.append(JobSpec(
            "intraday", "interval",
            seconds=max(1, int(c.get("intraday_interval_seconds", 3))),
            start_time=dtime(sh, sm), end_time=dtime(eh2, em2),
            description=JOB_DESCRIPTIONS.get("intraday", "")))

        # ETF T+0（底仓做T）：常挂日程，job 内部按 strategies.etf_t0.enabled
        # 决定是否真跑（enabled=false 返回 skipped）——WebUI「策略实验室」开关即启停。
        et0_sh, et0_sm = _parse_hm(c.get("etf_t0_start", "09:30"), (9, 30))
        et0_eh, et0_em = _parse_hm(c.get("etf_t0_end", "15:00"), (15, 0))
        specs.append(JobSpec(
            "etf_t0_intraday", "interval",
            seconds=max(1, int(c.get("etf_t0_interval_seconds", 30))),
            start_time=dtime(et0_sh, et0_sm), end_time=dtime(et0_eh, et0_em),
            description=JOB_DESCRIPTIONS.get("etf_t0_intraday", "")))

        # 个股存量持仓做T（高抛低吸，独立策略）：常挂日程，job 内部按
        # strategies.stock_t0.enabled 决定是否真跑（enabled=false 返回 skipped）。
        # 只对白名单里的已有持仓做日内先卖后买，不建仓、不净加仓、不净减仓，
        # 尾盘 T 仓强制归零；与主策略 / ETF T+0 完全独立。
        st0_sh, st0_sm = _parse_hm(c.get("stock_t0_start", "09:30"), (9, 30))
        st0_eh, st0_em = _parse_hm(c.get("stock_t0_end", "15:00"), (15, 0))
        specs.append(JobSpec(
            "stock_t0_intraday", "interval",
            seconds=max(1, int(c.get("stock_t0_interval_seconds", 30))),
            start_time=dtime(st0_sh, st0_sm), end_time=dtime(st0_eh, st0_em),
            description=JOB_DESCRIPTIONS.get("stock_t0_intraday", "")))

        # 尾盘选股法（独立短线策略）：始终挂在日程上，由 job 内部按
        # strategies.tail_pick.enabled 决定是否真跑（enabled=false 时返回 skipped）。
        # 时刻默认 14:30 / 09:30，可被 scheduler.jobs.tail_pick_select/exit 覆盖；
        # 改时刻需 reload 调度器（同其他 cron 任务），改 enabled 则下次触发即生效
        # （job 内部实时读 get_settings）。
        tp_sel = _parse_hm(c.get("tail_pick_select", "14:30"), (14, 30))
        tp_exit = _parse_hm(c.get("tail_pick_exit", "09:30"), (9, 30))
        specs.append(JobSpec("tail_pick_select", "cron", *tp_sel,
                             description=JOB_DESCRIPTIONS.get("tail_pick_select", "")))
        specs.append(JobSpec("tail_pick_exit", "cron", *tp_exit,
                             description=JOB_DESCRIPTIONS.get("tail_pick_exit", "")))

        # 策略实验室（独立策略）：同样常挂日程，job 内部按 strategies.<sid>.enabled
        # 决定跑不跑（enabled=false 返回 skipped）——WebUI「策略实验室」页的启用开关
        # 即运行/停止开关，勾选后下一次触发即生效（无需重启）。
        slb_open = _parse_hm(c.get("strategylab_open", "09:35"), (9, 35))
        slb_run = _parse_hm(c.get("strategylab_run", "14:45"), (14, 45))
        specs.append(JobSpec("strategylab_open", "cron", *slb_open,
                             description=JOB_DESCRIPTIONS.get("strategylab_open", "")))
        specs.append(JobSpec("strategylab_run", "cron", *slb_run,
                             description=JOB_DESCRIPTIONS.get("strategylab_run", "")))
        return specs

    def reload(self) -> bool:
        """重读配置并重建计划表（Web 页面改了执行时刻后调用）。

        APScheduler 下逐条换触发器；退化轮询模式下
        ``_loop`` 每轮遍历 ``self.specs``，整体替换列表即生效。
        """
        from ..core.config import get_settings
        # save_settings 落盘后已失效单例，这里重取才是最新 YAML；
        # 取不到（异常）时退回原实例，至少保证 specs 与之一致
        try:
            self.settings = get_settings()
        except Exception:                              # noqa: BLE001
            pass
        logger.info("调度计划重载：%s", self.settings.get("scheduler.jobs"))
        self.jobs_cfg = dict(self.settings.section("scheduler").get("jobs", {}) or {})
        self.specs = self._build_specs()
        self._fired.clear()
        if self._sched is not None:
            try:
                for spec in self.specs:
                    if spec.kind == "cron":
                        from apscheduler.triggers.cron import CronTrigger
                        trigger = CronTrigger(hour=spec.hour, minute=spec.minute,
                                              day_of_week=spec.day_of_week or "mon-sun",
                                              timezone=self.timezone)
                    else:
                        from apscheduler.triggers.interval import IntervalTrigger
                        trigger = IntervalTrigger(seconds=spec.seconds,
                                                  timezone=self.timezone)
                    self._sched.reschedule_job(spec.name, trigger=trigger)
            except Exception as exc:                     # noqa: BLE001
                logger.warning("调度计划热更新失败（重启后端后生效）: %s", exc)
                return False
        return True

    def describe(self) -> str:
        lines = [f"调度计划（tz={self.timezone}, enabled={self.enabled}）", "-" * 52]
        lines += ["  " + s.describe() for s in self.specs]
        return "\n".join(lines)

    # ------------------------------------------------------------ 触发
    def _fire(self, name: str) -> JobResult:
        from .jobs import run_job
        res = run_job(self.runner, name)
        self.results.append(res)
        logger.info("%s", res.render())
        return res

    def _guarded_fire(self, name: str) -> None:
        """APScheduler 的回调必须自己吞异常，否则线程池里的异常只会打日志然后消失。"""
        try:
            self._fire(name)
        except Exception:                            # noqa: BLE001
            logger.exception("调度触发 %s 失败", name)

    # ------------------------------------------------------------ APScheduler
    def _build_apscheduler(self):
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            from apscheduler.triggers.interval import IntervalTrigger
        except ImportError:
            return None

        sched = BackgroundScheduler(timezone=self.timezone)
        for spec in self.specs:
            if spec.kind == "cron":
                trigger = CronTrigger(hour=spec.hour, minute=spec.minute,
                                      day_of_week=spec.day_of_week or "mon-sun",
                                      timezone=self.timezone)
            else:
                trigger = IntervalTrigger(seconds=spec.seconds, timezone=self.timezone)
            sched.add_job(self._guarded_fire, trigger, args=[spec.name], id=spec.name,
                          misfire_grace_time=self.misfire, coalesce=True,
                          max_instances=1, replace_existing=True)
        # 机器休眠/卡顿时 cron 触发点超过宽限期，APScheduler 会直接丢弃该次执行——
        # 任务没跑就不会打心跳，超过 24h 后被体检误判「组件失联」降级 REDUCE_ONLY。
        # 错过 ≠ 失联：调度器还活着，补一条心跳防止健康检查误报。
        try:
            from apscheduler.events import EVENT_JOB_MISSED
            sched.add_listener(self._on_job_missed, EVENT_JOB_MISSED)
        except Exception:                            # noqa: BLE001
            logger.warning("注册 EVENT_JOB_MISSED 监听失败，错过补心跳失效")
        return sched

    def _on_job_missed(self, event) -> None:
        """APScheduler EVENT_JOB_MISSED 回调：任务被错过（多为机器休眠），补打心跳。"""
        try:
            name = str(getattr(event, "job_id", "") or "")
            if not name:
                return
            self.runner._beat(name)                  # noqa: SLF001 - 同包内可控
            logger.warning("调度任务 %s 被错过（机器休眠/卡顿？），已补心跳防体检误判失联", name)
        except Exception:                            # noqa: BLE001
            logger.exception("错过补心跳失败")

    def _beat_all(self) -> None:
        """进程启动即所有调度组件存活：补一轮心跳，清掉上一进程遗留的过期时间戳。"""
        for spec in self.specs:
            try:
                self.runner._beat(spec.name)         # noqa: SLF001
            except Exception:                        # noqa: BLE001
                pass

    # ------------------------------------------------------------ 退化轮询
    def _loop(self) -> None:
        """无 APScheduler 时的兜底循环。每秒醒一次，够精确了。"""
        last_intraday = 0.0
        while not self._stop.is_set():
            now = datetime.now()
            stamp = now.strftime("%Y-%m-%d %H:%M")
            for spec in self.specs:
                if spec.kind == "cron":
                    if now.hour != spec.hour or now.minute != spec.minute:
                        continue
                    if spec.day_of_week and _DOW[now.weekday()] != spec.day_of_week:
                        continue
                    if self._fired.get(spec.name) == stamp:
                        continue
                    self._fired[spec.name] = stamp
                    self._guarded_fire(spec.name)
                else:
                    t = now.time()
                    if spec.start_time and not (spec.start_time <= t <= spec.end_time):
                        continue
                    if time.time() - last_intraday < spec.seconds:
                        continue
                    last_intraday = time.time()
                    self._guarded_fire(spec.name)
            self._stop.wait(1.0)

    # ------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        if not self.enabled:
            logger.warning("调度器在配置中被禁用（scheduler.enabled=false）")
            return False
        self._sched = self._build_apscheduler()
        # 重启场景：上一次进程停机/机器休眠留下的过期心跳会让体检维持旧降级，
        # 调度器成功起来后先补一轮心跳，让下一轮体检自动恢复 NORMAL。
        self._beat_all()
        if self._sched is not None:
            self._sched.start()
            logger.info("APScheduler 已启动，共 %d 项任务", len(self.specs))
            return True
        logger.warning("未安装 APScheduler，退化为内置轮询调度")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sched", daemon=True)
        self._thread.start()
        return True

    def shutdown(self, wait: bool = True) -> None:
        if self._sched is not None:
            try:
                self._sched.shutdown(wait=wait)
            except Exception as exc:                 # noqa: BLE001
                logger.warning("调度器关闭异常: %s", exc)
            self._sched = None
        self._stop.set()
        if self._thread is not None and wait:
            self._thread.join(timeout=5)
            self._thread = None

    def run_forever(self) -> None:
        if not self.start():
            return
        try:
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            logger.info("收到退出信号，正在停止调度器…")
        finally:
            self.shutdown()

    # ------------------------------------------------------------ 回放
    def simulate_day(self, day: date | None = None) -> list[JobResult]:
        """把一天的任务按顺序跑一遍（不等真实时钟）。联调与冒烟用。"""
        if day is not None:
            self.runner._forced_date = day           # noqa: SLF001 - 同包内可控
        return self.runner.run_once_all()


def next_run_at(spec: JobSpec, now: datetime | None = None) -> datetime | None:
    """算出下一次触发时刻。仅用于展示，不参与实际调度。"""
    now = now or datetime.now()
    if spec.kind != "cron":
        t = now.time()
        if spec.start_time and spec.end_time:
            if spec.start_time <= t <= spec.end_time:
                return now + timedelta(seconds=spec.seconds)
            if t < spec.start_time:                    # 今天还没开盘
                return now.replace(hour=spec.start_time.hour,
                                   minute=spec.start_time.minute,
                                   second=0, microsecond=0)
            # 今天窗口已结束 → 明天开盘时（不精确排除周末/节假日，展示够用）
            nxt = now + timedelta(days=1)
            return nxt.replace(hour=spec.start_time.hour,
                               minute=spec.start_time.minute,
                               second=0, microsecond=0)
        return now + timedelta(seconds=spec.seconds)
    cand = now.replace(hour=spec.hour, minute=spec.minute, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    if spec.day_of_week:
        for _ in range(8):
            if _DOW[cand.weekday()] == spec.day_of_week:
                return cand
            cand += timedelta(days=1)
        return None
    return cand


__all__ = ["JobSpec", "TradingScheduler", "next_run_at", "JOB_DESCRIPTIONS"]
