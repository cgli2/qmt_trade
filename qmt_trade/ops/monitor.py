"""健康检查与自愈（设计 L6，P4 落地）。

系统在无人值守下跑，最危险的不是"出错"，而是**带病继续交易**：数据停更了还在下单、
QMT 断连了还以为挂单成功、对账不平却照常开新仓。

所以这里的定位不是"监控面板"，而是**决策闸门的输入**：
体检不合格 → 自动 `KillSwitch.engage()` 切 `REDUCE_ONLY`（只平不开），
而不是发条告警然后放任不管。这与 TradingAgents-CN 那种"只告警不阻断"的做法是本质区别。

每项检查都遵守：
- 检查自身抛异常 = 检查不通过（未知即危险，不能因为体检仪坏了就判定健康）；
- 有明确的 ``severity``，只有 ``ERROR/CRITICAL`` 才触发降级；
- 结果可序列化，直接进日报。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from ..core.logging import get_logger
from ..risk.killswitch import KillMode
from .notify import Level, Message, Notifier

logger = get_logger("ops.monitor")

#: 只有到达这个级别才会触发 KillSwitch 降级
DEGRADE_AT = Level.ERROR


@dataclass
class CheckResult:
    name: str
    ok: bool
    level: Level = Level.INFO
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def blocking(self) -> bool:
        """是否严重到应当停止开仓。"""
        return (not self.ok) and self.level >= DEGRADE_AT

    def render(self) -> str:
        mark = "OK  " if self.ok else f"{self.level.name:<5}"
        return f"[{mark}] {self.name:<18} {self.message}"


@dataclass
class HealthReport:
    at: datetime = field(default_factory=datetime.now)
    results: list[CheckResult] = field(default_factory=list)
    degraded: bool = False
    degrade_reasons: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok]

    def get(self, name: str) -> CheckResult | None:
        return next((r for r in self.results if r.name == name), None)

    def render(self) -> str:
        lines = ["=" * 58,
                 f"系统体检 {self.at:%Y-%m-%d %H:%M:%S}  "
                 f"{'健康' if self.healthy else '异常'}",
                 "=" * 58]
        lines += ["  " + r.render() for r in self.results]
        if self.degraded:
            lines.append("-" * 58)
            lines.append("  ⚠ 已自动降级 REDUCE_ONLY: " + "; ".join(self.degrade_reasons))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "healthy": self.healthy,
                "degraded": self.degraded,
                "results": [{"name": r.name, "ok": r.ok, "level": r.level.name,
                             "message": r.message, "detail": r.detail}
                            for r in self.results]}


class HealthMonitor:
    """健康检查器。

    组件用 ``heartbeat(name)`` 主动上报存活；``check()`` 做一次全量体检。
    ``killswitch`` 传入后，体检出现 blocking 项会自动降级。
    """

    def __init__(self, settings=None, *, repos=None, killswitch=None,
                 notifier: Notifier | None = None):
        cfg = (settings.section("ops").get("monitor", {}) if settings is not None else {})
        cfg = cfg if isinstance(cfg, dict) else {}
        self.settings = settings
        self.repos = repos
        self.killswitch = killswitch
        self.notifier = notifier
        self.heartbeat_seconds = float(cfg.get("heartbeat_seconds", 120))
        self.max_data_lag_days = int(cfg.get("max_data_lag_days", 3))
        self.max_llm_cost_ratio = float(cfg.get("max_llm_cost_ratio", 0.8))
        self.max_reject_ratio = float(cfg.get("max_reject_ratio", 0.3))
        self.min_disk_free_mb = float(cfg.get("min_disk_free_mb", 500))
        self._beats: dict[str, float] = {}
        self._custom: list[tuple[str, Callable[[], CheckResult]]] = []
        self.last: HealthReport | None = None
        # 常驻组件（如 intraday 每 30 秒一跳）用短阈值；job: 前缀是一次性
        # 任务（一天只跳一次），用 24 小时阈值，否则"昨晚跑过、今天还没到点"
        # 会被误判成失联 → KillSwitch 误拉闸，全天禁止开仓。
        self.cron_heartbeat_seconds = float(cfg.get("cron_heartbeat_seconds", 86400))

    # ------------------------------------------------------------ 心跳
    def heartbeat(self, component: str) -> None:
        now = time.time()
        self._beats[component] = now
        # 持久化到库：CLI/脚本等一次性进程做健康检查时，
        # 也能看到常驻调度进程最近是否真的在跑。
        if self.repos is not None:
            try:
                self.repos.system.set(f"hb:{component}", f"{now:.0f}")
            except Exception:                       # noqa: BLE001
                pass

    def register(self, name: str, fn: Callable[[], CheckResult]) -> None:
        """挂自定义检查项。"""
        self._custom.append((name, fn))

    # ------------------------------------------------------------ 各项检查
    def _check_heartbeats(self) -> CheckResult:
        now = time.time()
        # 本进程没打过心跳的组件，从库里回读（常驻进程上报的）
        if self.repos is not None:
            try:
                for k, v in self._read_db_beats().items():
                    self._beats.setdefault(k, v)
            except Exception:                       # noqa: BLE001
                pass
        if not self._beats:
            return CheckResult("heartbeat", True, Level.INFO, "无注册组件")
        stale = {}
        for k, t in self._beats.items():
            threshold = (self.cron_heartbeat_seconds if k.startswith("job:")
                         else self.heartbeat_seconds)
            if now - t > threshold:
                stale[k] = round(now - t, 1)
        if stale:
            return CheckResult("heartbeat", False, Level.ERROR,
                               f"{len(stale)} 个组件失联: {', '.join(stale)}",
                               {"stale": stale})
        return CheckResult("heartbeat", True, Level.INFO,
                           f"{len(self._beats)} 个组件正常")

    def _read_db_beats(self) -> dict[str, float]:
        out: dict[str, float] = {}
        rows = self.repos.system.list_prefix("hb:")
        for key, raw in rows.items():
            try:
                out[key[3:]] = float(raw)
            except (TypeError, ValueError):
                continue
        return out

    def _check_data_freshness(self, asof: date | None = None) -> CheckResult:
        """数据是否停更。停更后所有因子都是隔夜馊饭，必须停开仓。"""
        if self.repos is None:
            return CheckResult("data_freshness", True, Level.INFO, "未接数据库，跳过")
        try:
            row = self.repos.snapshots.latest()
        except Exception as exc:
            return CheckResult("data_freshness", False, Level.ERROR,
                               f"查询失败: {exc}")
        if not row:
            return CheckResult("data_freshness", True, Level.INFO, "尚无快照（首次运行）")
        try:
            last = datetime.strptime(str(row["trade_date"])[:10], "%Y-%m-%d").date()
        except Exception:
            return CheckResult("data_freshness", False, Level.WARN,
                               f"快照日期不可解析: {row.get('trade_date')}")
        lag = ((asof or date.today()) - last).days
        if lag > self.max_data_lag_days:
            return CheckResult("data_freshness", False, Level.ERROR,
                               f"数据落后 {lag} 天（上限 {self.max_data_lag_days}）",
                               {"last": last.isoformat(), "lag_days": lag})
        return CheckResult("data_freshness", True, Level.INFO,
                           f"最新 {last}（落后 {lag} 天）", {"lag_days": lag})

    def _check_llm_budget(self, asof: date | None = None) -> CheckResult:
        if self.repos is None or self.settings is None:
            return CheckResult("llm_budget", True, Level.INFO, "跳过")
        try:
            from ..brain.llm.registry import load_llm_config
            budget = float(load_llm_config().budget.get("daily_cny", 0) or 0)
        except Exception:
            budget = 0
        if budget <= 0:
            return CheckResult("llm_budget", True, Level.INFO, "未设日预算")
        try:
            spent = float(self.repos.llm_calls.cost_on(asof or date.today()))
        except Exception as exc:
            return CheckResult("llm_budget", False, Level.WARN, f"查询失败: {exc}")
        ratio = spent / budget
        if ratio >= 1.0:
            # 超预算不算 ERROR：P5 的设计就是降级成纯因子继续跑，不该停开仓
            return CheckResult("llm_budget", False, Level.WARN,
                               f"日预算已用尽 {spent:.2f}/{budget:.2f}，转纯因子模式",
                               {"spent": spent, "budget": budget, "ratio": ratio})
        if ratio >= self.max_llm_cost_ratio:
            return CheckResult("llm_budget", False, Level.INFO,
                               f"日预算已用 {ratio:.0%}（{spent:.2f}/{budget:.2f}）",
                               {"ratio": ratio})
        return CheckResult("llm_budget", True, Level.INFO,
                           f"成本 {spent:.2f}/{budget:.2f}", {"ratio": ratio})

    #: 业务预期内的拒单关键词：T+1 无可卖、限价未触达、防重/限频守护等。
    #: 这些是策略/规则的正常结果，不是"券商侧或参数出问题"，不计入异常。
    _EXPECTED_REJECTS = ("T+1", "无可卖", "限价未触达", "限频", "防重",
                         "无持仓可卖", "无行情", "KillSwitch")

    def _check_order_health(self, asof: date | None = None) -> CheckResult:
        """下单被拒比例。突然大量被拒 = 券商侧或参数出问题了。"""
        if self.repos is None:
            return CheckResult("order_health", True, Level.INFO, "跳过")
        try:
            rows = self.repos.orders.list_by_date(asof or date.today())
        except Exception as exc:
            return CheckResult("order_health", False, Level.WARN, f"查询失败: {exc}")
        if not rows:
            return CheckResult("order_health", True, Level.INFO, "今日无订单")
        bad = []
        for r in rows:
            if str(r.get("status")) not in ("REJECTED", "GUARD_BLOCKED", "FAILED"):
                continue
            if str(r.get("status")) == "GUARD_BLOCKED":
                continue                              # 守护拦截本身就是预期行为
            reason = str(r.get("reject_reason") or "")
            if any(k in reason for k in self._EXPECTED_REJECTS):
                continue                              # 业务预期拒单，不算异常
            bad.append(r)
        ratio = len(bad) / len(rows)
        if ratio > self.max_reject_ratio:
            return CheckResult("order_health", False, Level.ERROR,
                               f"下单异常比例 {ratio:.0%}（{len(bad)}/{len(rows)}）",
                               {"ratio": ratio, "rejected": len(bad), "total": len(rows)})
        return CheckResult("order_health", True, Level.INFO,
                           f"{len(rows)} 笔订单，异常 {len(bad)} 笔", {"ratio": ratio})

    def _check_reconcile(self, asof: date | None = None) -> CheckResult:
        """对账。Gate-3 不通过 → 次日禁止开仓，直到人工确认（设计 6.6.3）。"""
        if self.repos is None:
            return CheckResult("reconcile", True, Level.INFO, "跳过")
        try:
            row = self.repos.db.query_one(
                "SELECT * FROM reconcile_logs ORDER BY created_at DESC LIMIT 1")
        except Exception as exc:
            return CheckResult("reconcile", False, Level.WARN, f"查询失败: {exc}")
        if not row:
            return CheckResult("reconcile", True, Level.INFO, "尚无对账记录")
        if not int(row.get("passed") or 0):
            return CheckResult("reconcile", False, Level.CRITICAL,
                               f"{row.get('trade_date')} 对账未通过，禁止开仓",
                               {"detail": str(row.get("detail"))[:500]})
        return CheckResult("reconcile", True, Level.INFO,
                           f"{row.get('trade_date')} 对账通过")

    def _check_killswitch(self) -> CheckResult:
        if self.killswitch is None:
            return CheckResult("killswitch", True, Level.INFO, "未接入")
        mode = self.killswitch.mode.value
        if mode == "NORMAL":
            return CheckResult("killswitch", True, Level.INFO, "NORMAL")
        # 已经处于降级态：如实报告，但不重复触发降级（避免自激）
        return CheckResult("killswitch", False, Level.WARN,
                           f"当前 {mode}：{self.killswitch.reason or '-'}",
                           {"mode": mode})

    def _check_disk(self) -> CheckResult:
        try:
            path = str(self.settings.data_dir) if self.settings is not None else "."
            usage = shutil.disk_usage(path)
            free_mb = usage.free / 1024 / 1024
        except Exception as exc:
            return CheckResult("disk", False, Level.WARN, f"检查失败: {exc}")
        if free_mb < self.min_disk_free_mb:
            return CheckResult("disk", False, Level.ERROR,
                               f"磁盘剩余 {free_mb:.0f}MB < {self.min_disk_free_mb:.0f}MB",
                               {"free_mb": round(free_mb, 1)})
        return CheckResult("disk", True, Level.INFO, f"剩余 {free_mb/1024:.1f}GB",
                           {"free_mb": round(free_mb, 1)})

    # ------------------------------------------------------------ 全量体检
    def check(self, asof: date | None = None, *, notify: bool = True) -> HealthReport:
        rep = HealthReport()
        checks: list[Callable[[], CheckResult]] = [
            self._check_killswitch,
            lambda: self._check_data_freshness(asof),
            lambda: self._check_reconcile(asof),
            lambda: self._check_order_health(asof),
            lambda: self._check_llm_budget(asof),
            self._check_heartbeats,
            self._check_disk,
        ]
        checks += [fn for _n, fn in self._custom]

        for fn in checks:
            t0 = time.perf_counter()
            try:
                r = fn()
            except Exception as exc:               # 体检仪坏了 = 不健康
                name = getattr(fn, "__name__", "custom").lstrip("_").replace("check_", "")
                r = CheckResult(name, False, Level.ERROR, f"检查异常: {exc}")
                logger.exception("健康检查 %s 抛异常", name)
            r.elapsed_ms = (time.perf_counter() - t0) * 1000
            rep.results.append(r)

        # 自动降级：只平不开
        blockers = [r for r in rep.results if r.blocking]
        ks = self.killswitch
        already_degraded_by_health = (
            ks is not None and ks.mode is not KillMode.NORMAL
            and not ks.manual and "健康检查未通过" in str(ks.reason or ""))
        if blockers and ks is not None:
            reasons = [f"{r.name}: {r.message}" for r in blockers]
            rep.degraded, rep.degrade_reasons = True, reasons
            if not already_degraded_by_health:
                # 已经因体检降级时不重复 engage：原因里带计数/明细，每轮都在变，
                # 重复拉闸会被当作"新事件"反复推 CRITICAL；状态早已生效，
                # 日志留痕即可，告警只在首次降级时发一条。
                ks.engage("健康检查未通过 —— " + "; ".join(reasons))
                logger.error("体检不通过，已降级 REDUCE_ONLY: %s", reasons)
            else:
                logger.error("体检不通过，维持 REDUCE_ONLY: %s", reasons)
        elif (not blockers and ks is not None
              and ks.mode is not KillMode.NORMAL
              and not ks.manual
              and "健康检查未通过" in str(ks.reason or "")):
            # 自动恢复：上次是体检自动降级，本次体检已全部通过 → 解除降级。
            # 人工拉闸或其它触发源的降级不自动恢复，必须人工确认。
            logger.info("体检恢复通过，KillSwitch 由 REDUCE_ONLY 恢复 NORMAL")
            self.killswitch.set(KillMode.NORMAL, reason="健康检查恢复通过", manual=True)

        if notify and self.notifier is not None:
            self._push(rep)
        self.last = rep
        return rep

    def _push(self, rep: HealthReport) -> None:
        bad = rep.failed
        if not bad:
            self.notifier.send(Message(
                title="系统体检通过", level=Level.DEBUG, key="health:ok",
                body=f"{len(rep.results)} 项全部正常"))
            return
        top = max(bad, key=lambda r: r.level)
        self.notifier.send(Message(
            title=f"系统体检异常 {len(bad)}/{len(rep.results)} 项",
            body="\n".join(r.render() for r in bad),
            level=Level.CRITICAL if rep.degraded else top.level,
            key="health:bad",
            fields={"已降级": rep.degraded}))


class Watchdog:
    """盘中看门狗：把「多久没跑过一轮」变成可检测的信号。

    盘中循环每轮调用 ``tick()``；``expired()`` 为真说明循环卡死或线程死了。
    刻意做得极简——看门狗自己必须简单到不可能出错。
    """

    def __init__(self, name: str, *, timeout_seconds: float = 30.0,
                 monitor: HealthMonitor | None = None):
        self.name = name
        self.timeout = float(timeout_seconds)
        self.monitor = monitor
        self.last_tick = time.time()
        self.ticks = 0

    def tick(self) -> None:
        self.last_tick = time.time()
        self.ticks += 1
        if self.monitor is not None:
            self.monitor.heartbeat(self.name)

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_tick

    def expired(self) -> bool:
        return self.idle_seconds > self.timeout

    def as_check(self) -> CheckResult:
        if self.expired():
            return CheckResult(f"watchdog:{self.name}", False, Level.ERROR,
                               f"{self.idle_seconds:.0f}s 无心跳（上限 {self.timeout:.0f}s）",
                               {"ticks": self.ticks})
        return CheckResult(f"watchdog:{self.name}", True, Level.INFO,
                           f"{self.ticks} 轮，空闲 {self.idle_seconds:.0f}s")


def trading_window_guard(now: datetime, killswitch, *,
                         open_time: str = "09:30", close_time: str = "15:00") -> bool:
    """定时触发的 KillSwitch（设计 6.6.4「定时」触发源）。

    收盘后自动进 ``REDUCE_ONLY``，避免任何隔夜的意外开仓。返回是否在交易时段内。
    """
    hm = now.strftime("%H:%M")
    in_window = open_time <= hm <= close_time and now.weekday() < 5
    if not in_window and killswitch is not None and killswitch.mode.value == "NORMAL":
        killswitch.engage(f"非交易时段（{hm}），停止开仓")
    return in_window
