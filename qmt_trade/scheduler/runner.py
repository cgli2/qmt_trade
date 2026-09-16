"""APScheduler 封装：把 :class:`JobRunner` 的任务挂到时间表上。

两点刻意为之的设计：

1. **APScheduler 是可选依赖**。装了就用 ``BackgroundScheduler``；没装则退化为
   内置的轮询循环（精度秒级，够用）。调度器本身不该成为系统起不来的理由。
2. **盘中不是 cron 而是 interval**。09:30–15:00 每 N 秒巡检一次持仓，
   cron 表达不了"区间内高频"，用 interval + 时段判断更直白。

另有一条贯穿全模块的联动规则：**任务是否挂上日程 = 人工开关 AND 绑定策略门禁**。

- 人工开关：``scheduler.jobs.<name>_enabled``（默认 true），工作台「编辑调度任务」
  里的状态开关直接写它，见 :func:`manual_enabled`；
- 策略门禁：绑定关系见 ``JOB_STRATEGY_BINDING``，判定见 :func:`bound_strategy_enabled`；
- 合成判定见 :func:`job_enabled`。

任一不通过，任务连 job 都不注册（而不是注册后每次触发再 return skipped），从根上
消掉空跑开销。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable

from .jobs import CRITICAL_JOBS, JobResult, JobRunner

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

#: 任务 ↔ 独立策略绑定：绑定的策略**任一** ``strategies.<sid>.enabled=true`` 时任务才挂上日程。
#: 未登记的任务属于主管线（data_sync/regime/…/intraday/reconcile/review/evolve），恒启用——
#: 它们是系统骨架，不随任何单个策略开关起停。
#:
#: 为什么是"摘掉 job"而不是"job 内部 return skipped"：interval 任务交易日每 30 秒触发一次
#: （660 次/天），每次都要打心跳、抢 ``_strategy_boundary_lock``、跑一遍
#: ``StrategyRepository.apply_at_boundary`` 再打一行 SKIP 日志——策略没开时这些全是纯开销。
JOB_STRATEGY_BINDING: dict[str, tuple[str, ...]] = {
    "etf_t0_intraday": ("etf_t0",),
    "stock_t0_intraday": ("stock_t0",),
    "tail_pick_select": ("tail_pick",),
    "tail_pick_exit": ("tail_pick",),
    # open 相位只有打板/二板真下买单（低吸/趋势在尾盘 run 相位买）
    "strategylab_open": ("limit_up", "second_board"),
    # run 相位 = 尾盘买入（低吸/趋势）+ 四个策略的持仓管理 + 日收益入策略池
    "strategylab_run": ("limit_up", "second_board", "dip_buy", "trend_buy"),
}

#: cron 型任务名 → ``scheduler.jobs`` 下存"执行时刻"的键。
#: 任务名与配置键并非总是一致（``research`` 的历史键名是 ``llm_research``），
#: 编辑路由据此校验任务名并落盘，避免白名单在前后端各写一份后漂移。
JOB_TIME_KEYS: dict[str, str] = {
    "data_sync": "data_sync",
    "regime": "regime",
    "selection": "selection",
    "research": "llm_research",
    "plan": "plan",
    "auction_check": "auction_check",
    "reconcile": "reconcile",
    "review": "review",
    "evolve": "evolve",
    "tail_pick_select": "tail_pick_select",
    "tail_pick_exit": "tail_pick_exit",
    "strategylab_open": "strategylab_open",
    "strategylab_run": "strategylab_run",
}

#: interval 型任务名 → (窗口开始键, 窗口结束键, 间隔秒键)，同在 ``scheduler.jobs`` 下。
JOB_WINDOW_KEYS: dict[str, tuple[str, str, str]] = {
    "intraday": ("intraday_start", "intraday_end", "intraday_interval_seconds"),
    "etf_t0_intraday": ("etf_t0_start", "etf_t0_end", "etf_t0_interval_seconds"),
    "stock_t0_intraday": ("stock_t0_start", "stock_t0_end", "stock_t0_interval_seconds"),
}

#: interval 型任务的兜底周期（秒）。配置缺失时 ``_build_specs`` 与
#: :func:`job_interval_seconds` 必须取同一个值，否则触发器周期与"等锁预算"会
#: 各算各的——预算比真实周期还长时，一次调用就会跨到下轮触发。
JOB_INTERVAL_DEFAULTS: dict[str, int] = {
    "intraday": 3,
    "etf_t0_intraday": 30,
    "stock_t0_intraday": 30,
}


def job_interval_seconds(name: str, settings) -> int:
    """任务自身的触发周期（秒）。非 interval 型（cron）或计划表外 → ``0``。

    给 ``jobs.py`` 推导抢策略边界锁的等待预算用：预算必须**明显小于自身周期**，
    否则"等锁 + 干活"会跨到下轮触发，APScheduler 又要刷"实例数已达上限"。
    """
    keys = JOB_WINDOW_KEYS.get(name)
    if not keys:
        return 0
    default = JOB_INTERVAL_DEFAULTS.get(name, 30)
    if settings is None:
        return default
    try:
        return max(1, int(settings.get(f"scheduler.jobs.{keys[2]}", default)))
    except Exception:                                # noqa: BLE001
        return default


def bound_strategy_enabled(name: str, settings) -> bool:
    """策略门禁：无策略绑定 → 恒 True；有绑定 → 任一策略启用即 True。

    只看策略开关，不看人工开关；要判"任务到底跑不跑"请用 :func:`job_enabled`。
    """
    sids = JOB_STRATEGY_BINDING.get(name)
    if not sids or settings is None:
        return True
    for sid in sids:
        try:
            if bool(settings.get(f"strategies.{sid}.enabled", False)):
                return True
        except Exception:                            # noqa: BLE001
            continue
    return False


def manual_enabled(name: str, settings) -> bool:
    """工作台里的人工开关：``scheduler.jobs.<name>_enabled``，缺省视为开。

    与策略门禁是 **AND** 关系 —— 人工关掉就不挂日程；策略没开时人工开着也不挂。
    配置读不出来时一律回落到"开"：漏跑一个任务比多跑一个更容易被发现。
    """
    if settings is None:
        return True
    try:
        return bool(settings.get(f"scheduler.jobs.{name}_enabled", True))
    except Exception:                                # noqa: BLE001
        return True


def job_enabled(name: str, settings) -> bool:
    """任务是否该挂上日程 = 人工开关 AND 绑定策略门禁。

    这是**唯一**该被外部（jobs 的心跳门禁、路由校验）引用的判定入口。
    """
    return manual_enabled(name, settings) and bound_strategy_enabled(name, settings)


def _parse_hm(text: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = str(text).split(":")
        return int(h), int(m)
    except Exception:                                # noqa: BLE001
        logger.warning("时间格式非法 %r，改用 %02d:%02d", text, *default)
        return default


#: 调度任务状态码全集 —— ``JobSpec.status`` 的取值域，也是前端 ``labels.ts`` 里
#: ``JOB_STATE`` 字典必须覆盖的键。i18n 校验脚本直接从这一行抓取值（见
#: ``tests/check_i18n_enum_coverage.py`` 的 LITERAL_MAP），所以新增状态时改这里
#: 就等于同时给校验脚本下了新指标：labels.ts 没补中文译文，校验立刻失败。
JOB_STATES: tuple[str, ...] = ("running", "paused", "disabled")

#: 状态码 → ``describe()`` 里的标记后缀（纯文本计划表的"停用"提示）。
_JOB_STATE_MARKS: dict[str, str] = {
    "running": "",
    "paused": "  [人工暂停]",
    "disabled": "  [停用]",
}

# 两张表必须同步：漏一个状态，describe() 会在拼计划表时 KeyError。放在导入期炸，
# 比等到调度器启动、或用户点开工作台页面才炸要好定位得多。
_missing = set(JOB_STATES) ^ set(_JOB_STATE_MARKS)
if _missing:
    raise RuntimeError(f"JOB_STATES 与 _JOB_STATE_MARKS 不一致，差异：{sorted(_missing)}")
del _missing


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
    #: 人工开关（工作台「编辑调度任务」里的状态开关）；False = 用户主动停用
    manual_enabled: bool = True
    #: 绑定策略门禁是否通过（无绑定 → 恒 True）
    strategy_gate: bool = True
    #: 生效启用 = manual_enabled AND strategy_gate。False → 调度器不注册该 job
    enabled: bool = True
    #: 绑定的独立策略 id（见 JOB_STRATEGY_BINDING）；空 = 主管线任务，不受策略门禁管
    bound_strategies: tuple[str, ...] = ()

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

    def in_window(self, now: datetime) -> bool:
        """当前时刻是否落在该计划的巡检窗口内。

        ``start_time``/``end_time`` 只有 interval 型任务会配（如盘中巡检 09:30–15:00）；
        cron 型任务的时刻由触发器自己表达，恒 True。

        **APScheduler 路径必须显式判这一条**：``IntervalTrigger`` 只认"每 N 秒"，
        窗口信息传不进去（``start_date``/``end_date`` 是一次性时间点，表达不了每日
        复现的窗口）。此前只有退化轮询 ``_loop`` 判窗口，于是 30 秒一 tick 的巡检
        任务在夜里也照触发——每 tick 都要抢策略边界锁、跑一遍
        ``apply_at_boundary``（DuckDB 写）、再打一行 SKIP，纯烧资源；长任务持锁时
        还会让三个巡检任务一起排队，APScheduler 随即每 30 秒刷一条
        "maximum number of running instances reached"。
        """
        if self.kind != "interval" or not self.start_time or not self.end_time:
            return True
        t = now.time()
        return self.start_time <= t <= self.end_time

    @property
    def status(self) -> str:
        """UI 展示用状态码：running（在跑）/ paused（人工暂停）/ disabled（随策略停用）。

        两道闸门都关时显示 ``paused``：人工开关是用户自己拨的、也在他手边，
        优先提示这个才有可操作性（策略门禁的原因写进 ``status_detail``）。
        """
        if self.enabled:
            return "running"
        return "paused" if not self.manual_enabled else "disabled"

    @property
    def status_detail(self) -> str:
        """停用原因（启用时为空串）。UI 挂 tooltip，让用户知道去哪儿恢复。"""
        if self.enabled:
            return ""
        reasons = []
        if not self.manual_enabled:
            reasons.append("已在工作台手动暂停")
        if not self.strategy_gate and self.bound_strategies:
            reasons.append("绑定策略均未启用：" + "、".join(self.bound_strategies))
        return "；".join(reasons) or "已停用"

    def describe(self) -> str:
        return f"{self.name:<14} {self.kind:<8} {self.time_label}{_JOB_STATE_MARKS[self.status]}"


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
            seconds=job_interval_seconds("intraday", self.settings),
            start_time=dtime(sh, sm), end_time=dtime(eh2, em2),
            description=JOB_DESCRIPTIONS.get("intraday", "")))

        # ETF T+0（底仓做T）：strategies.etf_t0.enabled=false 时整条计划被摘掉
        # （不再每 30 秒空跑一遍）——WebUI「策略实验室」开关即启停，改完 reload 生效。
        et0_sh, et0_sm = _parse_hm(c.get("etf_t0_start", "09:30"), (9, 30))
        et0_eh, et0_em = _parse_hm(c.get("etf_t0_end", "15:00"), (15, 0))
        specs.append(JobSpec(
            "etf_t0_intraday", "interval",
            seconds=job_interval_seconds("etf_t0_intraday", self.settings),
            start_time=dtime(et0_sh, et0_sm), end_time=dtime(et0_eh, et0_em),
            description=JOB_DESCRIPTIONS.get("etf_t0_intraday", "")))

        # 个股存量持仓做T（高抛低吸，独立策略）：strategies.stock_t0.enabled=false
        # 时整条计划被摘掉。只对白名单里的已有持仓做日内先卖后买，不建仓、不净加仓、
        # 不净减仓，尾盘 T 仓强制归零；与主策略 / ETF T+0 完全独立。
        st0_sh, st0_sm = _parse_hm(c.get("stock_t0_start", "09:30"), (9, 30))
        st0_eh, st0_em = _parse_hm(c.get("stock_t0_end", "15:00"), (15, 0))
        specs.append(JobSpec(
            "stock_t0_intraday", "interval",
            seconds=job_interval_seconds("stock_t0_intraday", self.settings),
            start_time=dtime(st0_sh, st0_sm), end_time=dtime(st0_eh, st0_em),
            description=JOB_DESCRIPTIONS.get("stock_t0_intraday", "")))

        # 尾盘选股法（独立短线策略）：strategies.tail_pick.enabled=false 时两条计划
        # 一起摘掉（选股与离场同生同灭，不会只关一半留下裸持仓）。
        # 时刻默认 14:30 / 09:30，可被 scheduler.jobs.tail_pick_select/exit 覆盖；
        # 改时刻与改 enabled 都要 reload 调度器（tail_pick 路由已自动 reload）。
        tp_sel = _parse_hm(c.get("tail_pick_select", "14:30"), (14, 30))
        tp_exit = _parse_hm(c.get("tail_pick_exit", "09:30"), (9, 30))
        specs.append(JobSpec("tail_pick_select", "cron", *tp_sel,
                             description=JOB_DESCRIPTIONS.get("tail_pick_select", "")))
        specs.append(JobSpec("tail_pick_exit", "cron", *tp_exit,
                             description=JOB_DESCRIPTIONS.get("tail_pick_exit", "")))

        # 策略实验室（独立策略）：open/run 两个相位各绑定一组策略
        # （见 JOB_STRATEGY_BINDING），组内全关就摘掉对应相位。WebUI「策略实验室」页
        # 的启用开关即运行/停止开关，保存后路由会 reload 调度器，无需重启。
        slb_open = _parse_hm(c.get("strategylab_open", "09:35"), (9, 35))
        slb_run = _parse_hm(c.get("strategylab_run", "14:45"), (14, 45))
        specs.append(JobSpec("strategylab_open", "cron", *slb_open,
                             description=JOB_DESCRIPTIONS.get("strategylab_open", "")))
        specs.append(JobSpec("strategylab_run", "cron", *slb_run,
                             description=JOB_DESCRIPTIONS.get("strategylab_run", "")))
        self._publish_intervals(specs)
        return self._apply_gates(specs)

    def _publish_intervals(self, specs: list[JobSpec]) -> None:
        """把 interval 型任务的**真实触发周期**挂到 ctx，供 ``jobs.py`` 算等锁预算。

        为什么不让 jobs.py 自己读配置：``jobs_cfg`` 是快照，而预算要到触发那一刻
        才读。工作台改了周期、调度器还没 reload 时触发器仍是旧周期——预算若按新
        周期算就可能长过真实周期，一次调用跨到下轮触发，APScheduler 又要刷
        ``maximum number of running instances``。以 spec 为准才不会错配。
        """
        ctx = getattr(getattr(self, "runner", None), "ctx", None)
        if ctx is None:                                 # 裸构造（测试）时没有 ctx
            return
        ctx._job_intervals = {s.name: s.seconds          # noqa: SLF001
                              for s in specs if s.kind == "interval"}

    def _apply_gates(self, specs: list[JobSpec]) -> list[JobSpec]:
        """给每条计划打两道闸门标记，合成出最终 ``enabled``。

        - ``manual_enabled``：工作台「编辑调度任务」里的人工开关；
        - ``strategy_gate``：绑定策略是否有任一启用（见 JOB_STRATEGY_BINDING）。

        specs 始终包含**全部**任务（UI 要展示"哪些被停用了"以及停用原因），
        真正的过滤发生在注册/触发环节 —— 见 ``_build_apscheduler`` / ``_loop``。
        """
        for spec in specs:
            spec.bound_strategies = JOB_STRATEGY_BINDING.get(spec.name, ())
            spec.manual_enabled = manual_enabled(spec.name, self.settings)
            spec.strategy_gate = bound_strategy_enabled(spec.name, self.settings)
            spec.enabled = spec.manual_enabled and spec.strategy_gate
        return specs

    def reload(self) -> bool:
        """重读配置并重建计划表（Web 页面改了执行时刻/状态/策略启停后调用）。

        退化轮询模式下 ``_loop`` 每轮遍历 ``self.specs``，整体替换列表即生效；
        APScheduler 下必须**增删改三管齐下**：改时刻用 reschedule，任务停用要
        remove_job（否则继续空跑），重新启用要 add_job（reschedule 一个
        不存在的 job 会抛 JobLookupError，把整轮 reload 拖成失败）。
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
        old = {s.name: s for s in self.specs}
        self.specs = self._build_specs()
        self._fired.clear()
        self._sync_beats()
        if self._sched is None:
            return True
        try:
            self._sync_apscheduler(old)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("调度计划热更新失败（重启后端后生效）: %s", exc)
            return False
        return True

    def _sync_apscheduler(self, old: dict[str, JobSpec]) -> None:
        """把 APScheduler 里的 job 集合对齐到 ``self.specs`` 中启用的那部分。"""
        new = {s.name: s for s in self.specs}
        for name in set(old) - set(new):             # 计划表里彻底消失的任务
            self._remove_job(name)
        for name, spec in new.items():
            if not spec.enabled:
                self._remove_job(name, spec.status_detail)
                continue
            prev = old.get(name)
            if prev is not None and prev.enabled:    # 原本就挂着 → 只换触发器
                self._sched.reschedule_job(name, trigger=self._trigger_of(spec))
            else:                                    # 新任务 / 刚被重新启用 → 挂上去
                self._add_job(spec)
        logger.info("调度计划已对齐：启用 %d 项，停用 %d 项",
                    sum(1 for s in self.specs if s.enabled),
                    sum(1 for s in self.specs if not s.enabled))

    def _remove_job(self, name: str, reason: str = "已停用") -> None:
        """摘除一个 job。本来就不存在（一直没启用过）是正常情况，静默跳过。"""
        try:
            from apscheduler.jobstores.base import JobLookupError
        except ImportError:                            # pragma: no cover
            return
        try:
            self._sched.remove_job(name)
        except JobLookupError:
            return
        logger.info("已摘除调度任务 %s（%s）", name, reason)

    def _trigger_of(self, spec: JobSpec):
        """按 spec 造 APScheduler 触发器。cron → CronTrigger，其余 → IntervalTrigger。"""
        if spec.kind == "cron":
            from apscheduler.triggers.cron import CronTrigger
            return CronTrigger(hour=spec.hour, minute=spec.minute,
                               day_of_week=spec.day_of_week or "mon-sun",
                               timezone=self.timezone)
        from apscheduler.triggers.interval import IntervalTrigger
        return IntervalTrigger(seconds=spec.seconds, timezone=self.timezone)

    def _add_job(self, spec: JobSpec, trigger=None) -> None:
        """把一条启用的计划挂上 APScheduler（依赖 ``self._sched`` 已就位）。

        ``name=spec.name`` 不是装饰：APScheduler 自己打的告警（如"实例数已达上限，
        本次跳过"）用的是 ``Job.name``，而所有任务共用同一个回调
        ``_guarded_fire``，不设 name 时告警里全是 ``TradingScheduler._guarded_fire``
        ——三个 30 秒巡检任务刷出来的告警长得一模一样，根本归不到是哪个任务。
        """
        self._sched.add_job(self._guarded_fire, trigger or self._trigger_of(spec),
                            args=[spec.name], id=spec.name, name=spec.name,
                            misfire_grace_time=self.misfire, coalesce=True,
                            max_instances=1, replace_existing=True)

    def describe(self) -> str:
        lines = [f"调度计划（tz={self.timezone}, enabled={self.enabled}）", "-" * 52]
        lines += ["  " + s.describe() for s in self.specs]
        return "\n".join(lines)

    # ------------------------------------------------------------ 触发
    def _spec_of(self, name: str) -> JobSpec | None:
        """按任务名取计划。找不到返回 None（如 reload 后 job 尚未摘干净的残留触发）。"""
        for spec in self.specs:
            if spec.name == name:
                return spec
        return None

    def _fire(self, name: str) -> JobResult:
        from .jobs import run_job
        res = run_job(self.runner, name)
        self.results.append(res)
        logger.info("%s", res.render())
        return res

    def _guarded_fire(self, name: str, *, enforce_window: bool = True) -> None:
        """APScheduler 的回调必须自己吞异常，否则线程池里的异常只会打日志然后消失。

        ``enforce_window``：interval 任务是否受巡检窗口约束。正常触发一律受约束
        （见 :meth:`JobSpec.in_window`）；``_on_job_missed`` 的补跑传 False ——
        那是"机器休眠错过了整段窗口"的一次性补救决定，任务体自己还有
        ``session.is_continuous`` 守卫，不该在这里被窗口再拦一次。
        """
        if enforce_window:
            spec = self._spec_of(name)
            if spec is not None and not spec.in_window(datetime.now()):
                # 静默返回：不抢边界锁、不写 job_runs、不打 INFO。窗口外每 30 秒
                # 刷一条 SKIP 正是"一堆空跑"的观感来源，而它对排查毫无价值。
                logger.debug("任务 %s 不在巡检窗口（%s）内，本次触发跳过",
                             name, spec.time_label)
                return
        try:
            self._fire(name)
        except Exception:                            # noqa: BLE001
            logger.exception("调度触发 %s 失败", name)

    # ------------------------------------------------------------ APScheduler
    def _build_apscheduler(self):
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError:
            return None

        sched = BackgroundScheduler(timezone=self.timezone)
        # _add_job 依赖 self._sched，必须先落字段再挂任务
        self._sched = sched
        for spec in self.specs:
            if not spec.enabled:                     # 人工暂停 / 绑定策略全关 → 不注册
                logger.info("调度任务 %s 未挂上日程（%s）", spec.name, spec.status_detail)
                continue
            self._add_job(spec)
        # 机器休眠/卡顿时 cron 触发点超过宽限期，APScheduler 会直接丢弃该次执行——
        # 任务没跑就不会打心跳，超过 24h 后被体检误判「组件失联」降级 REDUCE_ONLY。
        # 分两种处理：关键任务（data_sync/reconcile/intraday）错过 = 真实缺口，只补
        # 心跳会掩盖问题（数据没预热却显示"活着"），故直接补跑本体；其余任务补心跳
        # 防误报即可（详见 _on_job_missed）。
        try:
            from apscheduler.events import EVENT_JOB_MISSED
            sched.add_listener(self._on_job_missed, EVENT_JOB_MISSED)
        except Exception:                            # noqa: BLE001
            logger.warning("注册 EVENT_JOB_MISSED 监听失败，错过补救失效")
        return sched

    def _on_job_missed(self, event) -> None:
        """APScheduler EVENT_JOB_MISSED 回调：任务被错过（多为机器休眠/卡顿）。

        - **关键任务**（CRITICAL_JOBS：data_sync/reconcile/intraday）：错过是真实
          数据/对账缺口，只补心跳会掩盖问题——体检看着"活着"，实则当日行情根本没
          预热。故直接补跑本体：``_fire`` 内部走 ``run_job`` → ``@job`` 包装器，会
          自动打心跳、留痕、并在失败时按 CRITICAL 拉闸，与正常触发完全一致。任务
          本体自带守卫（非交易日跳过、空样本判失败），补跑安全。
        - **非关键任务**（research/plan/evolve 等）：错过多为时序性问题，在错误时点
          补跑可能触发副作用（如盘中跑盘前任务），维持原「只补心跳防体检误判失联」。
        """
        try:
            name = str(getattr(event, "job_id", "") or "")
            if not name:
                return
            if name in CRITICAL_JOBS:
                logger.warning("关键任务 %s 被错过（机器休眠/卡顿？），补跑本体", name)
                self._guarded_fire(name, enforce_window=False)
            else:
                self.runner._beat(name)              # noqa: SLF001 - 同包内可控
                logger.warning("调度任务 %s 被错过，已补心跳防体检误判失联", name)
        except Exception:                            # noqa: BLE001
            logger.exception("错过补救失败")

    def _beat_all(self) -> None:
        """进程启动即所有**启用中的**调度组件存活：补一轮心跳，清掉上一进程遗留的过期时间戳。

        停用的任务不补心跳，反而要主动注销（见 ``_sync_beats``）。
        """
        self._sync_beats()
        for spec in self.specs:
            if not spec.enabled:
                continue
            try:
                self.runner._beat(spec.name)         # noqa: SLF001
            except Exception:                        # noqa: BLE001
                pass

    def _sync_beats(self) -> None:
        """注销停用任务的心跳，防止体检把它们误判为"组件失联"。

        ``HealthMonitor._check_heartbeats`` 会把库里所有 ``hb:job:*`` 都当活组件，
        超过 24h 没跳就算失联 → ERROR（blocking）→ 自动降级 REDUCE_ONLY。任务停用
        （人工暂停或随策略摘除）后不再触发、自然不再打心跳，残留的时间戳就会在
        一天后把"关了个任务"放大成"全系统禁止开仓"。
        """
        for spec in self.specs:
            if spec.enabled:
                continue
            try:
                self.runner.ctx.monitor.forget(f"job:{spec.name}")
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
                if not spec.enabled:
                    continue
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
                    if not spec.in_window(now):
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
            logger.info("APScheduler 已启动：启用 %d 项任务，停用 %d 项（绑定策略未启用）",
                        sum(1 for s in self.specs if s.enabled),
                        sum(1 for s in self.specs if not s.enabled))
            return True
        logger.warning("未安装 APScheduler，退化为内置轮询调度（启用 %d 项，停用 %d 项）",
                       sum(1 for s in self.specs if s.enabled),
                       sum(1 for s in self.specs if not s.enabled))
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


__all__ = ["JobSpec", "TradingScheduler", "next_run_at", "JOB_DESCRIPTIONS",
           "JOB_STRATEGY_BINDING", "JOB_TIME_KEYS", "JOB_WINDOW_KEYS",
           "JOB_INTERVAL_DEFAULTS", "job_interval_seconds",
           "bound_strategy_enabled", "manual_enabled", "job_enabled"]
