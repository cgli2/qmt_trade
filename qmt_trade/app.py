"""应用装配容器（Composition Root）。

整个系统只有这一处知道"谁依赖谁"。jobs / cli / 回测脚本都从这里取组件，
避免各处重复 new 一遍导致状态分裂（比如两个 KillSwitch 实例，拉了闸另一边不知道）。

两种运行模式（``mode``）：

===========  ==================  ==================  =========================
mode         数据源               撮合                 用途
===========  ==================  ==================  =========================
``paper``    真实 provider        SimGateway          真数据模拟盘，验证策略
``live``     真实 provider        QMTGateway          实盘，真金白银
===========  ==================  ==================  =========================

P4 失败安全在这里就开始生效：``live`` 模式下装配不出真实数据源或网关，
不会"退而求其次用假数据"，而是直接拉 KillSwitch 并抛错——
拿 Mock 行情下真单是这套系统能犯的最严重的错误。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .core.clock import TradingCalendar
from .core.config import Settings, get_settings
from .core.trading import Position
from .storage.db import Database
from .storage.models import Repos
from .risk.killswitch import KillMode, KillSwitch

logger = logging.getLogger(__name__)

#: system_state 中持久化 KillSwitch 的键
KILL_STATE_KEY = "kill_switch_mode"
KILL_REASON_KEY = "kill_switch_reason"
KILL_MANUAL_KEY = "kill_switch_manual"

VALID_MODES = ("paper", "live")


def _load_json_list(raw: Any) -> list:
    """从 positions 表还原 JSON 列表列；脏数据一律当空处理（P4）。"""
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:                                  # noqa: BLE001
        return []


class ContextError(RuntimeError):
    """装配失败。实盘下属于致命错误，不允许降级继续。"""


class TradingContext:
    """组件容器。所有属性懒加载，用到才装配。

    ``close()`` 后不要再复用；需要新环境请重新构造（测试里尤其重要，
    否则 SQLite 连接会跨用例串味）。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        mode: str = "paper",
        db_path: str | Path | None = None,
        # 以下均为测试/回测注入点，生产不传
        repos: Repos | None = None,
        hub: Any = None,
        gateway: Any = None,
        notifier: Any = None,
        killswitch: KillSwitch | None = None,
        providers: list | None = None,
        initial_cash: float | None = None,
    ):
        if mode not in VALID_MODES:
            raise ContextError(f"未知运行模式 {mode!r}，可选 {VALID_MODES}")
        self.mode = mode
        self.settings = settings if settings is not None else get_settings()
        self.calendar = TradingCalendar()

        self._db_path = db_path
        self._repos = repos
        self._hub = hub
        self._gateway = gateway
        self._notifier = notifier
        self._killswitch = killswitch
        self._providers_override = providers
        self._initial_cash = initial_cash

        self._db: Database | None = None
        self._shared_repos: Repos | None = None
        self._shared_db: Database | None = None
        self._portfolio = None
        self._cost = None
        self._guard = None
        self._risk = None
        self._sizer = None
        self._exec = None
        self._pipeline = None
        self._brain = None
        self._reconciler = None
        self._reporter = None
        self._monitor = None
        self._pool = None
        self._closed = False

    # ================================================================ 基础设施
    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def db(self) -> Database:
        if self._db is None:
            if self._repos is not None:            # 外部注入了 repos，复用其 db
                self._db = self._repos.db
            else:
                path = self._db_path
                if path is None:
                    # 账本物理隔离：live 与 paper 分库，模拟盘数据绝不污染实盘账本
                    name = "trade_live.db" if self.is_live else "trade.db"
                    path = self.settings.data_dir / name
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                self._db = Database(path)
        return self._db

    @property
    def repos(self) -> Repos:
        if self._repos is None:
            self._repos = Repos.create(self.db)
        return self._repos

    @property
    def shared_repos(self) -> Repos:
        """决策产物共享账本。

        账本库按模式物理隔离（防模拟盘污染实盘持仓/订单），但选股候选、
        研判精选、观察池、regime 等**决策产物与模式无关**——常驻调度器
        只以 paper 运行，产物若随账本隔离，live 读到的永远是空库：
        页面选股为空、plan 拿不到 Intent、research 防重失效重复烧 LLM。
        因此决策产物统一读写共享的 trade.db；paper 即 repos 本身。
        账本类表（positions/orders/plans/snapshots）仍走 repos 保持隔离。
        """
        if self._shared_repos is not None:
            return self._shared_repos
        if self._repos is not None or self._db_path is not None or not self.is_live:
            # 外部注入 repos / 指定了 db_path（测试、回测）：不另开共享库
            self._shared_repos = self.repos
            return self._shared_repos
        path = self.settings.data_dir / "trade.db"
        self._shared_db = Database(path)
        self._shared_repos = Repos.create(self._shared_db)
        return self._shared_repos

    @property
    def notifier(self):
        if self._notifier is None:
            from .ops.notify import build_notifier
            self._notifier = build_notifier(self.settings)
        return self._notifier

    @property
    def killswitch(self) -> KillSwitch:
        """KillSwitch 单例，状态跨重启持久化。

        重启后如果上次是 REDUCE_ONLY/FLATTEN，**必须恢复成同样的档位**——
        否则"昨晚对账没过拉了闸，今早重启自动满血复活"就是灾难。
        """
        if self._killswitch is None:
            ks = KillSwitch()
            try:
                saved = self.repos.system.get(KILL_STATE_KEY)
                if saved and saved != KillMode.NORMAL.value:
                    reason = self.repos.system.get(KILL_REASON_KEY) or "重启前遗留状态"
                    # manual 也要带回来：「人主动停的」和「系统自动停的」在复盘时
                    # 是两件完全不同的事，恢复成默认值等于丢失关键信息
                    manual = str(self.repos.system.get(KILL_MANUAL_KEY) or "") == "1"
                    ks.set(KillMode(saved), reason=f"重启恢复：{reason}", manual=manual)
                    logger.warning("KillSwitch 从持久化状态恢复为 %s（%s，%s）",
                                   saved, reason, "人工" if manual else "自动")
            except Exception as exc:                # 读不到状态也不能当作正常
                logger.error("读取 KillSwitch 持久化状态失败: %s", exc)
                ks.set(KillMode.REDUCE_ONLY, reason=f"状态读取失败:{type(exc).__name__}")
            ks.on_change(self._on_kill_change)
            self._killswitch = ks
        return self._killswitch

    def _on_kill_change(self, ks: KillSwitch) -> None:
        """KillSwitch 变档时落库 + 告警。本身绝不能抛（P4）。"""
        try:
            self.repos.system.set(KILL_STATE_KEY, ks.mode.value, reason=ks.reason)
            self.repos.system.set(KILL_REASON_KEY, ks.reason or "")
            self.repos.system.set(KILL_MANUAL_KEY, "1" if ks.manual else "0")
        except Exception as exc:
            logger.error("KillSwitch 状态落库失败: %s", exc)
        try:
            # 非交易时段（收盘后/节假日）本来就不可能开仓，降级状态如实落库即可，
            # 再推 CRITICAL 只会半夜刷屏；真正的异常留到开盘时段再告警。
            if not self.calendar.session_of(datetime.now()).is_tradable:
                logger.info("KillSwitch → %s（非交易时段，不推送告警）: %s",
                            ks.mode.value, ks.reason or "-")
                return
            level = "CRITICAL" if ks.mode is not KillMode.NORMAL else "WARN"
            self.notifier.notify(f"KillSwitch → {ks.mode.value}", ks.reason or "",
                                 level=level, key="killswitch")
        except Exception as exc:
            logger.warning("KillSwitch 变更通知失败: %s", exc)

    # ================================================================ 数据层
    def _build_providers(self) -> list:
        if self._providers_override is not None:
            return list(self._providers_override)

        out: list = []
        errors: list[str] = []
        # 按配置里出现过的源去重装配，不可用的直接跳过（各源本身也有熔断）
        wanted: list[str] = []
        for names in (self.settings.section("datahub.priority") or {}).values():
            for n in names or ():
                if n not in wanted:
                    wanted.append(n)

        for name in wanted:
            try:
                p = _make_provider(name, self.settings)
            except Exception as exc:                # noqa: BLE001
                errors.append(f"{name}:{type(exc).__name__}")
                continue
            if p is None:
                continue
            if not p.is_available():
                errors.append(f"{name}:不可用")
                continue
            out.append(p)

        if not out:
            # paper/live 绝不使用 Mock 行情。无真实数据源即 fail-loud，
            # 宁可起不来，也绝不拿假数据默默跑出"业绩"（对齐 P4 实盘 fail-closed）。
            msg = "无可用数据源（" + ("; ".join(errors) or "配置为空") + "）"
            logger.error(msg)
            if self.is_live:
                self.killswitch.engage(msg)
            raise ContextError(msg)
        elif errors:
            logger.warning("部分数据源不可用：%s", "; ".join(errors))
        return out

    @property
    def hub(self):
        if self._hub is None:
            from .datahub.manager import DataHub
            self._hub = DataHub(self.settings, self._build_providers())
        return self._hub

    # ================================================================ 执行层
    @property
    def cost(self):
        if self._cost is None:
            from .execution.costs import CostModel
            self._cost = CostModel.from_settings(self.settings)
        return self._cost

    @property
    def gateway(self):
        if self._gateway is None:
            if self.is_live:
                from .execution.gateway.qmt import QMTGateway
                self._gateway = QMTGateway(self.settings, killswitch=self.killswitch,
                                           notifier=self.notifier)
            else:
                from .execution.gateway.simulator import SimGateway
                self._gateway = SimGateway()
        return self._gateway

    @property
    def portfolio(self):
        """组合状态。实盘从数据库还原，回测/模拟用初始现金起步。"""
        if self._portfolio is None:
            from .portfolio.state import PortfolioState
            cash = self._initial_cash
            if cash is None:
                cash = float(self.settings.get("backtest.initial_cash", 1_000_000) or 0)
            snap = None
            try:
                snap = self.repos.snapshots.latest()
            except Exception as exc:
                logger.error("读取账户快照失败: %s", exc)
                if self.is_live:
                    self.killswitch.engage(f"账户快照读取失败:{type(exc).__name__}")
            if snap:
                cash = float(snap["cash"])
            ps = PortfolioState(cash=cash)
            if snap:
                ps.initial_asset = float(snap["total_asset"]) or cash
                ps.peak_asset = ps.initial_asset
            self._restore_positions(ps)
            self._portfolio = ps
        return self._portfolio

    def _restore_positions(self, ps) -> None:
        """从 positions 表还原持仓。读失败在实盘下必须拉闸——
        持仓不全会让风控以为可以随便开新仓。"""
        try:
            rows = self.repos.positions.list_all()
        except Exception as exc:
            logger.error("读取持仓失败: %s", exc)
            if self.is_live:
                self.killswitch.engage(f"持仓读取失败:{type(exc).__name__}")
            return
        for r in rows:
            vol = int(r.get("volume") or 0)
            if vol <= 0:
                continue
            entry = r.get("entry_date")
            ps.positions[r["symbol"]] = Position(
                symbol=r["symbol"],
                shares=vol,
                can_use=int(r.get("available") or 0),
                avg_cost=float(r.get("avg_cost") or 0),
                industry=str(r.get("industry") or ""),
                opened_at=date.fromisoformat(str(entry)[:10]) if entry else None,
                highest_since_open=float(r.get("highest_price") or 0),
                last_price=float(r.get("last_price") or 0),
                stop_loss_price=float(r["stop_loss_price"]) if r.get("stop_loss_price") else None,
                stop_loss_type=str(r.get("stop_loss_type") or ""),
                take_profit=_load_json_list(r.get("take_profit")),
                invalidation_checks=_load_json_list(r.get("invalidation_checks")),
                max_holding_days=int(r["max_holding_days"] or 0) if r.get("max_holding_days") else 0,
                tp_done_levels=[int(x) for x in _load_json_list(r.get("tp_done_levels"))],
                origin_shares=int(r.get("origin_shares") or 0),
            )

    @property
    def guard(self):
        if self._guard is None:
            from .execution.order_guard import OrderGuard
            self._guard = OrderGuard(self.settings)
        return self._guard

    @property
    def risk(self):
        if self._risk is None:
            from .risk.engine import RiskEngine
            # 注入 hub：Gate-2 逻辑止损需要算均线（回测另建 RiskEngine 可不传）
            self._risk = RiskEngine(self.settings, hub=self.hub)
        return self._risk

    @property
    def sizer(self):
        if self._sizer is None:
            from .portfolio.sizer import PositionSizer
            self._sizer = PositionSizer(self.settings)
        return self._sizer

    @property
    def execution(self):
        if self._exec is None:
            from .execution.service import ExecutionService
            self._exec = ExecutionService(
                self.settings, self.gateway, self.cost, self.guard, self.risk,
                self.killswitch, self.portfolio, sizer=self.sizer,
                repos=self.repos)
        return self._exec

    # ================================================================ 决策层
    @property
    def pipeline(self):
        if self._pipeline is None:
            from .selection.pipeline import SelectionPipeline
            self._pipeline = SelectionPipeline(self.settings, self.hub)
        return self._pipeline

    @property
    def brain(self):
        if self._brain is None:
            from .brain.graph import build_brain
            b = build_brain(self.settings, self.hub)
            client = getattr(b, "client", None)
            if client is not None and hasattr(client, "audit"):
                client.audit = self._audit_llm_call
            self._brain = b
        return self._brain

    def _audit_llm_call(self, kw: dict) -> None:
        """LLM 真实调用审计落库（llm_calls 表，P6 可审计）。

        只由 LLMManager 在实调成功后回调（缓存命中不回调），
        因此表里的 cost 合计即真实花费。落库失败由 manager 兜住，不影响研判。
        """
        import re
        m = re.search(r"SYMBOL:\s*(\S+)", kw.get("prompt") or "")
        kw["symbol"] = m.group(1) if m else None
        self.repos.llm_calls.save(**kw)

    # ================================================================ 运维层
    @property
    def reconciler(self):
        if self._reconciler is None:
            from .execution.reconcile import Reconciler
            self._reconciler = Reconciler(self.settings, repos=self.repos,
                                          killswitch=self.killswitch,
                                          notifier=self.notifier)
        return self._reconciler

    @property
    def reporter(self):
        if self._reporter is None:
            from .ops.report import Reporter
            self._reporter = Reporter(self.settings, repos=self.repos,
                                      notifier=self.notifier)
        return self._reporter

    @property
    def monitor(self):
        if self._monitor is None:
            from .ops.monitor import HealthMonitor
            self._monitor = HealthMonitor(self.settings, repos=self.repos,
                                          killswitch=self.killswitch,
                                          notifier=self.notifier)
            self._monitor.register("datahub", self._check_hub)
        return self._monitor

    def _check_hub(self):
        from .ops.monitor import CheckResult
        from .ops.notify import Level
        try:
            ok = self.hub.is_healthy()
            snap = self.hub.health_snapshot()
        except Exception as exc:                    # noqa: BLE001
            return CheckResult("datahub", False, Level.ERROR, f"数据源体检异常:{exc}")
        dead = [s.get("name") for s in snap if not s.get("healthy", True)]
        return CheckResult("datahub", bool(ok),
                           Level.INFO if ok else Level.ERROR,
                           "全部正常" if ok else f"异常源:{dead}")

    @property
    def pool(self):
        """策略池。状态存在 system_state 里，跨重启保留权重。"""
        if self._pool is None:
            from .evolution.pool import StrategyPool
            p = StrategyPool(self.settings)
            try:
                raw = self.repos.system.get("strategy_pool")
                if raw:
                    import json
                    p.load(json.loads(raw))
            except Exception as exc:
                logger.warning("策略池状态还原失败，使用空池: %s", exc)
            self._pool = p
        return self._pool

    def save_pool(self) -> bool:
        if self._pool is None:
            return False
        try:
            import json
            self.repos.system.set("strategy_pool",
                                  json.dumps(self._pool.snapshot(), ensure_ascii=False,
                                             default=str),
                                  reason="策略池调权")
            return True
        except Exception as exc:
            logger.error("策略池状态落库失败: %s", exc)
            return False

    # ================================================================ 生命周期
    def persist_portfolio(self, trade_date: date | None = None) -> bool:
        """把内存组合写回数据库。盘后必须调用，否则重启后账本回到上次快照。"""
        d = trade_date or date.today()
        ps = self.portfolio
        try:
            existing = {r["symbol"] for r in self.repos.positions.list_all()}
            for sym, pos in ps.positions.items():
                self.repos.positions.upsert(
                    sym,
                    volume=int(pos.shares),
                    available=int(pos.can_use),
                    avg_cost=float(pos.avg_cost),
                    last_price=float(pos.last_price or pos.highest_since_open or pos.avg_cost),
                    entry_date=pos.opened_at.isoformat() if pos.opened_at else None,
                    highest_price=float(pos.highest_since_open or 0),
                    stop_loss_price=pos.stop_loss_price,
                    stop_loss_type=pos.stop_loss_type or None,
                    take_profit=json.dumps(pos.take_profit, ensure_ascii=False, default=str)
                    if pos.take_profit else None,
                    max_holding_days=pos.max_holding_days or None,
                    invalidation_checks=json.dumps(pos.invalidation_checks, ensure_ascii=False)
                    if pos.invalidation_checks else None,
                    tp_done_levels=json.dumps(pos.tp_done_levels or []),
                    origin_shares=int(pos.origin_shares or 0) or None,
                    industry=pos.industry or None,
                )
            for sym in existing - set(ps.positions):
                self.repos.positions.remove(sym)
            self.repos.snapshots.save(
                d,
                total_asset=ps.total_asset, cash=ps.cash,
                market_value=ps.position_value,
                realized_pnl=ps.day_realized,
                position_count=len(ps.positions),
            )
            return True
        except Exception as exc:
            logger.error("组合落库失败: %s", exc)
            try:
                self.repos.risk_events.add("SYS", "PERSIST_FAIL", str(exc),
                                           severity="ERROR", trade_date=d)
            except Exception:                       # noqa: BLE001 - 兜底不再抛
                pass
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for obj, name in ((self._gateway, "gateway"),
                          (self._shared_db, "shared_db"), (self._db, "db")):
            closer = getattr(obj, "close", None)     # SimGateway 没有 close，属正常
            if closer is None:
                continue
            try:
                closer()
            except Exception as exc:                # noqa: BLE001
                logger.warning("关闭 %s 失败: %s", name, exc)

    def __enter__(self) -> "TradingContext":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _make_provider(name: str, settings=None):
    """按名字造 provider。未知名字返回 None（配置写错不该让系统起不来）。"""
    if name == "qmt":
        from .datahub.providers.qmt_provider import QmtProvider
        # 财务跨进程磁盘缓存：目录与 TTL 对齐 datahub.cache.fundamental_ttl，
        # 避免每次重跑回测 / walk_forward 都重新触发 QMT 全市场财务下载。
        cache_dir = str(settings.data_dir / "fundamentals") if settings is not None else None
        ttl = 86400
        if settings is not None:
            ttl = int((settings.section("datahub") or {}).get("cache", {})
                      .get("fundamental_ttl", 86400))
        return QmtProvider(cache_dir=cache_dir, fundamental_ttl=ttl)
    if name == "tushare":
        from .datahub.providers.tushare_provider import TushareProvider
        return TushareProvider()
    if name == "akshare":
        from .datahub.providers.akshare_provider import AkshareProvider
        # 批量基本面报表的磁盘缓存目录（历史报告期内容不再变化，可长期复用）
        cache_dir = str(settings.data_dir / "akshare_cache") if settings is not None else None
        # 新闻逐票拉取并行度：默认 8（I/O 密集，8 线程把 4500+ 只首轮从 ~2h 降到 ~20min）；
        # 若东方财富接口限流（大量超时/空返回），可调小或调大。
        news_workers = 8
        if settings is not None:
            news_workers = int((settings.section("datahub") or {}).get("news_workers", 8))
        return AkshareProvider(cache_dir=cache_dir, news_workers=news_workers)
    if name == "mock":
        from .datahub.providers.mock import MockProvider
        return MockProvider()
    logger.warning("未知数据源 %s，已跳过", name)
    return None


def build_context(mode: str = "paper", *, settings: Settings | None = None, **kw) -> TradingContext:
    """便捷工厂。CLI 与 jobs 的统一入口。"""
    return TradingContext(settings, mode=mode, **kw)


__all__ = ["TradingContext", "build_context", "ContextError", "VALID_MODES",
           "KILL_STATE_KEY", "KILL_REASON_KEY", "KILL_MANUAL_KEY"]
