"""L7 调度层：一个交易日的完整任务链。

日程（可在 ``scheduler.jobs`` 覆盖时刻）::

    06:30 data_sync      预热行情/基本面，数据体检
    07:30 regime         市场状态判定，决定当日仓位上限
    08:00 selection      L0 硬过滤 + L1 因子排序 → 候选池
    08:15 research       L2 多智能体研判 → TradeIntent（落库）
    09:00 plan           Intent → 交易计划（落库，等开盘执行）
    09:20 auction_check  集合竞价复核：停牌/一字板/跳空过大的计划作废
    09:30 intraday       盘中循环：Gate-2 持仓守护 + 执行待办计划
    15:05 reconcile      Gate-3 盘后对账（不通过 → REDUCE_ONLY）
    16:00 review         复盘归因 + 日报
    周日  evolve         walk-forward 寻优 + 策略池调权 + 周报 + 阶段分析报告

设计要点：

* **每个 job 都是隔离的**。任何异常都被 :func:`job` 装饰器接住，转成
  ``JobResult(ok=False)``，绝不让一个任务崩掉整个调度器（P4）。
* **critical 任务失败会拉 KillSwitch**。数据同步、对账这类失败意味着
  "系统已经不知道自己在干什么"，此时只允许减仓。
* **每次执行都留痕**。``system_state`` 里记 ``job:<name>:last_run`` /
  ``:last_status`` / ``:last_error``，重启后能看出上次跑到哪一步（P6）。
* **状态在数据库，不在内存**。选股结果、Intent、计划全部落库，
  盘中进程重启后可以从 ``plans`` 表接着做。
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from ..app import TradingContext

logger = logging.getLogger(__name__)

#: 失败即视为"系统失去掌控"的任务，会自动降级到 REDUCE_ONLY
CRITICAL_JOBS = frozenset({"data_sync", "reconcile", "intraday"})


def _candidateset_picks(cs, top_n: int = 50) -> list[dict]:
    """从 CandidateSet 抽 Top N 推荐明细（rank/score/industry），供 UI 展示。"""
    out: list[dict] = []
    frame = getattr(getattr(cs, "ranking", None), "frame", None)
    if frame is None or getattr(frame, "empty", True):
        return out
    for _, row in frame.head(top_n).iterrows():
        score = row.get("score", 0) or 0
        try:
            score = round(float(score), 4)
        except (TypeError, ValueError):
            score = 0.0
        out.append({
            "symbol": row.get("symbol"),
            "rank": int(row.get("rank", 0) or 0),
            "score": score if __import__("math").isfinite(score) else 0.0,
            "industry": row.get("industry", "") or "",
        })
    return out


def _ic_weight_overrides(settings, repos) -> dict[str, float]:
    """聚合近期因子 IC 历史，产出持续负 IC 因子的权重覆盖。

    降权条件刻意保守：回看窗口内样本数达标且**全部为负**才动手——
    IC 是噪声很大的统计量，单次负值不构成降权依据。权重随负 IC
    幅度线性衰减（1.0 → 0.2 封顶），不清零：留观察窗口，等它恢复。
    """
    cfg = settings.section("evolution") or {}
    lookback = int(cfg.get("ic_lookback", 20))
    min_obs = int(cfg.get("ic_min_obs", 5))
    thr = float(cfg.get("ic_downweight_threshold", -0.05))
    keys = repos.system.list_keys("evolution:factor_ic:")[-lookback:]
    if not keys:
        return {}
    hist: dict[str, list[float]] = {}
    for k in keys:
        try:
            ic = json.loads(repos.system.get(k) or "{}")
        except Exception:                            # noqa: BLE001
            continue
        for f, v in ic.items():
            if not f.endswith("_q"):                 # 只处理因子级；类别权重另走配置
                continue
            try:
                hist.setdefault(f, []).append(float(v))
            except (TypeError, ValueError):
                continue
    out: dict[str, float] = {}
    for f, vs in hist.items():
        if len(vs) < min_obs or max(vs) > 0:
            continue
        mean = sum(vs) / len(vs)
        if mean >= thr:
            continue
        out[f] = round(max(0.2, 1.0 + mean * 4), 4)
    if out:
        logger.info("IC 回灌：%d 个因子降权 %s", len(out), out)
    return out


@dataclass
class JobResult:
    name: str
    ok: bool = True
    skipped: bool = False
    reason: str = ""
    elapsed: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        if self.skipped:
            return f"[SKIP] {self.name:<14} {self.reason}"
        mark = "OK  " if self.ok else "FAIL"
        extra = " ".join(f"{k}={v}" for k, v in self.data.items() if not isinstance(v, (list, dict)))
        return f"[{mark}] {self.name:<14} {self.elapsed:6.2f}s {extra} {self.reason}".rstrip()

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "skipped": self.skipped,
                "reason": self.reason, "elapsed": round(self.elapsed, 3), "data": self.data}


def job(name: str, *, critical: bool | None = None):
    """把一个方法包装成"永不抛异常、自动留痕"的调度任务。

    被包装方法可以返回 ``JobResult``、``dict``（并入 data）或 ``None``。
    """
    def deco(fn: Callable[..., Any]):
        @functools.wraps(fn)
        def wrapper(self: "JobRunner", *args, **kw) -> JobResult:
            t0 = time.perf_counter()
            self._beat(name)
            try:
                raw = fn(self, *args, **kw)
            except Exception as exc:                # noqa: BLE001 - 调度层是最后一道防线
                elapsed = time.perf_counter() - t0
                res = JobResult(name, ok=False, reason=f"{type(exc).__name__}: {exc}",
                                elapsed=elapsed)
                logger.exception("任务 %s 执行失败", name)
                self._on_failure(res, critical=critical)
            else:
                if isinstance(raw, JobResult):
                    res = raw
                    res.name = name
                elif isinstance(raw, dict):
                    res = JobResult(name, data=raw)
                else:
                    res = JobResult(name)
                res.elapsed = time.perf_counter() - t0
                if not res.ok:
                    self._on_failure(res, critical=critical)
            self._record(res)
            self.history.append(res)
            return res
        wrapper.job_name = name                      # type: ignore[attr-defined]
        return wrapper
    return deco


class JobRunner:
    """持有 :class:`TradingContext`，按日程执行任务。

    所有任务都可以单独调用（CLI ``run --once <job>``），彼此之间通过
    **数据库** 而非内存传递状态，所以顺序错乱或中途重启都不会脏掉账本。
    """

    def __init__(self, ctx: TradingContext, *, trade_date: date | None = None,
                 dry_run: bool = False):
        self.ctx = ctx
        self.dry_run = dry_run
        self._forced_date = trade_date
        self.history: list[JobResult] = []
        #: 当日运行态缓存（进程内），丢了也能从库里重建
        self.cache: dict[str, Any] = {}

    # ============================================================ 基础设施
    @property
    def today(self) -> date:
        return self._forced_date or date.today()

    def _beat(self, name: str) -> None:
        try:
            self.ctx.monitor.heartbeat(f"job:{name}")
        except Exception:                            # noqa: BLE001
            pass

    def _record(self, res: JobResult) -> None:
        """任务留痕。写库失败只记日志——留痕失败不该反过来弄挂业务。"""
        try:
            sys_repo = self.ctx.repos.system
            sys_repo.set(f"job:{res.name}:last_run", datetime.now().isoformat(timespec="seconds"))
            sys_repo.set(f"job:{res.name}:last_status",
                         "SKIP" if res.skipped else ("OK" if res.ok else "FAIL"),
                         reason=res.reason[:200])
            if not res.ok:
                sys_repo.set(f"job:{res.name}:last_error", res.reason[:500])
        except Exception as exc:                     # noqa: BLE001
            logger.warning("任务留痕失败 %s: %s", res.name, exc)
        # 历史表：每次调度一行，观察期证据链（system_state 只存最后一次）
        try:
            self.ctx.repos.job_runs.add(
                res.name, "SKIP" if res.skipped else ("OK" if res.ok else "FAIL"),
                trade_date=self.today, reason=res.reason,
                elapsed=res.elapsed)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("job_runs 落库失败 %s: %s", res.name, exc)

    def _on_failure(self, res: JobResult, *, critical: bool | None) -> None:
        is_critical = CRITICAL_JOBS.__contains__(res.name) if critical is None else critical
        try:
            self.ctx.repos.risk_events.add(
                "SYS", f"JOB_FAIL:{res.name}", res.reason,
                severity="CRITICAL" if is_critical else "ERROR", trade_date=self.today)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("失败事件落库失败: %s", exc)
        if is_critical:
            try:
                self.ctx.killswitch.engage(f"关键任务 {res.name} 失败：{res.reason[:120]}")
            except Exception as exc:                 # noqa: BLE001
                logger.error("拉闸失败（这很糟）: %s", exc)
        try:
            self.ctx.notifier.notify(
                f"任务失败 {res.name}", res.reason,
                level="CRITICAL" if is_critical else "ERROR", key=f"job:{res.name}")
        except Exception as exc:                     # noqa: BLE001
            logger.warning("失败告警发送失败: %s", exc)

    def _skip_non_trading(self, name: str) -> JobResult | None:
        if self.ctx.calendar.is_trading_day(self.today):
            return None
        info = self.ctx.calendar.info(self.today)
        return JobResult(name, skipped=True, reason=f"非交易日（{info.reason}）")

    # ============================================================ 06:30 数据同步
    @job("data_sync")
    def data_sync(self) -> JobResult:
        """预热当日所需数据并体检。数据脏 → 拉闸，绝不用脏数据做决策。"""
        skip = self._skip_non_trading("data_sync")
        if skip:
            return skip

        hub = self.ctx.hub
        hub.set_asof(None)                            # 实盘不做时间切片
        instruments = hub.get_instruments()
        syms = ([i.symbol for i in instruments] if not isinstance(instruments, dict)
                else list(instruments))
        if not syms:
            return JobResult("data_sync", ok=False, reason="标的全集为空")

        start = self.today - timedelta(days=180)
        probe = syms[: min(len(syms), 20)]            # 抽样体检，不必全量拉
        df = hub.get_bars(probe, start=start, end=self.today)
        rows = 0 if df is None else len(df)
        quality = hub.validate_bars(df) if df is not None else None
        if rows == 0:
            return JobResult("data_sync", ok=False, reason="抽样行情为空")
        if quality is not None and not quality.ok:
            return JobResult("data_sync", ok=False,
                             reason="数据质量不合格: " + str(quality)[:200])

        healthy = hub.is_healthy()
        self.cache["universe"] = syms
        return JobResult("data_sync", ok=bool(healthy),
                         reason="" if healthy else "存在熔断中的数据源",
                         data={"symbols": len(syms), "rows": rows})

    # ============================================================ 07:30 市场状态
    @job("regime")
    def regime(self) -> JobResult:
        skip = self._skip_non_trading("regime")
        if skip:
            return skip
        snap = self.ctx.pipeline.detector.detect(self.today)
        self.cache["regime"] = snap
        try:
            self.ctx.shared_repos.system.set("regime:latest",
                                             json.dumps(snap.to_dict(), ensure_ascii=False),
                                             reason=snap.reason[:200])
        except Exception as exc:                     # noqa: BLE001
            logger.warning("Regime 落库失败: %s", exc)
        if snap.regime.value == "RISK_OFF":
            self.ctx.notifier.notify("市场进入 RISK_OFF", snap.reason,
                                     level="WARN", key=f"regime:{self.today}")
        return JobResult("regime", data={"regime": snap.regime.value,
                                         "max_position": snap.max_position,
                                         "degraded": snap.degraded})

    # ============================================================ 08:00 选股
    @job("selection")
    def selection(self) -> JobResult:
        skip = self._skip_non_trading("selection")
        if skip:
            return skip
        watch = self._load_watchlist()
        self.cache["watchlist"] = watch
        universe = self.cache.get("universe")
        if universe is not None and watch:           # 观察池标的必须在候选全集内
            uni = set(universe)
            universe = list(universe) + [s for s in watch if s not in uni]
        ic_overrides = _ic_weight_overrides(self.ctx.settings, self.ctx.shared_repos)
        cs = self.ctx.pipeline.run(self.today, universe=universe,
                                   extra_symbols=list(watch.keys()) or None,
                                   ic_overrides=ic_overrides or None)
        self.cache["candidates"] = cs
        try:
            payload = cs.to_dict()
            payload["picks"] = _candidateset_picks(cs)
            payload["watchlist"] = list(watch.keys())
            self.ctx.shared_repos.system.set(
                "selection:latest",
                json.dumps(payload, ensure_ascii=False, default=str),
                reason=f"{cs.n} 只候选")
        except Exception as exc:                     # noqa: BLE001
            logger.warning("候选池落库失败: %s", exc)
        try:
            self._save_selection_frame(cs)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("因子截面落库失败: %s", exc)
        return JobResult("selection", data={"n": cs.n,
                                            "watch": len(watch),
                                            "regime": cs.regime.regime.value,
                                            "degraded": len(cs.degraded),
                                            "ic_overrides": len(ic_overrides)})

    def _save_selection_frame(self, cs) -> None:
        """因子截面（symbol/score/各因子分位）按日落库，供盘后复盘算因子 IC。

        只留 ``*_q``/``cat_*``/score 列：原始因子值对 IC 无用（IC 算的是
        分位与收益的秩相关），裁掉能把存储体积降一个量级。
        """
        frame = getattr(cs, "frame", None)
        if frame is None or getattr(frame, "empty", True):
            return
        keep = [c for c in frame.columns
                if c in ("symbol", "score") or c.endswith("_q") or c.startswith("cat_")]
        records = frame[keep].round(6).to_dict(orient="records")
        self.ctx.shared_repos.system.set(
            f"selection:frame:{self.today.isoformat()}",
            json.dumps(records, ensure_ascii=False, default=str),
            reason=f"{len(records)} 只因子截面")
        # 清理过期截面，避免 system_state 无限膨胀
        cfg = self.ctx.settings.section("evolution") or {}
        cutoff = self.today - timedelta(days=int(cfg.get("frame_retention_days", 90)))
        for key in self.ctx.shared_repos.system.list_keys("selection:frame:"):
            try:
                if date.fromisoformat(key.rsplit(":", 1)[-1]) < cutoff:
                    self.ctx.shared_repos.system.delete(key)
            except ValueError:
                continue

    # ============================================================ 08:15 深度研判
    @job("research")
    def research(self) -> JobResult:
        skip = self._skip_non_trading("research")
        if skip:
            return skip
        return self.research_candidates(self.cache.get("candidates"))

    def research_candidates(self, cs, *, force: bool = False) -> JobResult:
        """research 核心逻辑（调度任务与手动 API 触发共用）。

        与调度入口的唯一区别：不做非交易日跳过、候选池由调用方显式传入
        （手动入口可从 selection:latest 落库结果重建，不依赖进程内缓存）。

        当日防重：同一候选池（asof+symbols 签名）当天已成功研判过则直接跳过，
        避免 catchup/手动 API/重启后重复触发全量 LLM 烧钱。``force=True`` 强制重跑。
        """
        if cs is None:
            return JobResult("research", skipped=True, reason="无候选池（selection 未运行）")
        if cs.is_empty:
            return JobResult("research", skipped=True, reason="候选池为空")

        sig_src = f"{cs.asof}|" + ",".join(cs.symbols or [])
        sig = hashlib.sha256(sig_src.encode()).hexdigest()[:16]
        done_key = f"research:done:{self.today}"
        prev = None
        try:
            raw = self.ctx.shared_repos.system.get(done_key)
            prev = json.loads(raw) if raw else None
        except Exception as exc:                     # noqa: BLE001
            logger.warning("research 防重状态读取失败（按未跑处理）: %s", exc)
        if not force and prev and prev.get("sig") == sig:
            logger.info("research 跳过：当日已对相同候选池完成研判 sig=%s（防重复烧钱）", sig)
            return JobResult("research", skipped=True,
                             reason="当日已对相同候选池完成研判，跳过以避免重复 LLM 开销"
                                    "（如需强制重跑请用 force=true）",
                             data={k: prev.get(k) for k in
                                   ("intents", "picks", "llm_calls", "llm_cached", "cost_cny")})

        snapshot = self._portfolio_snapshot(cs)
        lessons = self._recent_lessons()
        result = self.ctx.brain.run(cs, snapshot, lessons=lessons)
        self.cache["intents"] = result.intents
        self.cache["picks"] = result.picks

        stored = 0
        for it in result.intents:
            try:
                self.ctx.shared_repos.intents.add(it)
                stored += 1
            except Exception as exc:                 # noqa: BLE001
                logger.warning("Intent 落库失败 %s: %s", it.symbol, exc)

        self._store_picks(result)
        carried = self._update_watchlist(result)
        data = {"intents": len(result.intents),
                "picks": len(result.picks),
                "stored": stored,
                "watch": carried,
                "rejected": len(result.rejected),
                "lessons": len(lessons),
                "llm_calls": result.llm_calls,
                "llm_cached": result.llm_cached,
                "cost_cny": round(result.llm_cost_cny, 4)}
        try:                                           # 写防重签名，同日同池不再重跑
            self.ctx.shared_repos.system.set(
                done_key,
                json.dumps({**data, "sig": sig, "asof": str(cs.asof)}, ensure_ascii=False),
                reason="research 当日完成标记（防重复 LLM 开销）")
        except Exception as exc:                       # noqa: BLE001
            logger.warning("research 防重状态写入失败: %s", exc)
        return JobResult("research", data=data)

    def _recent_lessons(self, *, days: int = 10, limit: int = 8) -> list[str]:
        """近 N 天复盘沉淀的 WARN/CRITICAL 经验，回灌分析师 prompt（L5→L2 闭环）。"""
        try:
            rows = self.ctx.shared_repos.experiences.recent(
                self.today - timedelta(days=days), limit=limit, tags_like="WARN")
        except Exception as exc:                     # noqa: BLE001
            logger.warning("经验回灌检索失败: %s", exc)
            return []
        out: list[str] = []
        for r in rows:
            txt = str(r.get("lesson") or r.get("situation") or "").strip()
            if txt and txt not in out:
                out.append(txt)
        return out

    def _store_picks(self, result) -> None:
        """最终精选落库（P6 可审计）+ 写 system_state 供 UI 展示。落库失败不影响主流程。"""
        picks = result.picks
        picks_min = int(self.ctx.settings.get("brain.final_picks_min", 3) or 3)
        if len(picks) < picks_min:
            logger.warning("最终精选 %d 只，低于目标下限 %d（宁缺毋滥，不降标准凑数）",
                           len(picks), picks_min)
        try:
            self.ctx.shared_repos.picks.clear_date(self.today)
            for i, pk in enumerate(picks, 1):
                it = pk.intent
                self.ctx.shared_repos.picks.add(
                    trade_date=self.today, symbol=pk.symbol, rank=i,
                    action=pk.action, conviction=pk.conviction,
                    confidence=pk.confidence, industry=pk.industry,
                    reason=pk.reason,
                    votes=json.dumps(pk.votes, ensure_ascii=False),
                    payload=it.model_dump_json() if it is not None else "{}",
                    bull_case=pk.bull_case or "",
                    bear_case=pk.bear_case or "",
                    debate=json.dumps(pk.debate, ensure_ascii=False),
                    evidence=json.dumps(pk.evidence, ensure_ascii=False),
                )
        except Exception as exc:                     # noqa: BLE001
            logger.warning("最终精选落库失败: %s", exc)
        try:
            regime_snap = self.cache.get("regime")
            regime_name = getattr(getattr(regime_snap, "regime", None), "value", "") if regime_snap else ""
            payload = {
                "asof": self.today.isoformat(),
                "regime": regime_name,
                "n": len(picks),
                "picks": [pk.to_dict() for pk in picks],
            }
            self.ctx.shared_repos.system.set(
                "selection:final",
                json.dumps(payload, ensure_ascii=False, default=str),
                reason=f"{len(picks)} 只最终精选")
        except Exception as exc:                     # noqa: BLE001
            logger.warning("精选状态写入失败: %s", exc)

    # ------------------------------------------------------------ 滚动观察池
    def _load_watchlist(self) -> dict[str, dict]:
        """滚动观察池（system_state 持久化，重启不丢）。

        结构：``{symbol: {first_date, days, last_score, source}}``。
        观察池让昨日精选/高分候选次日继续被研判，优中选优靠多日
        连续验证而非一次性打分。
        """
        try:
            raw = self.ctx.shared_repos.system.get("selection:watchlist")
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except Exception as exc:                     # noqa: BLE001
            logger.warning("观察池读取失败: %s", exc)
            return {}

    def _update_watchlist(self, result) -> int:
        """研判后滚动更新观察池：存量超龄/已持仓的出局，
        当日精选与高置信 Intent 入池续研。失败不影响主流程。"""
        cfg = self.ctx.settings.section("brain.rolling") or {}
        carry_top = int(cfg.get("carry_top_n", 5))
        watch_days = int(cfg.get("watch_days", 5))
        min_score = float(cfg.get("min_score", 0.62))
        max_watch = int(cfg.get("max_watch", 20))

        prev = self.cache.get("watchlist")
        if prev is None:
            prev = self._load_watchlist()
        held = set(self.ctx.portfolio.positions)
        iso_today = self.today.isoformat()

        new: dict[str, dict] = {}
        retired: list[str] = []
        for sym, info in prev.items():
            if not isinstance(info, dict):
                info = {}
            days = int(info.get("days", 0)) + 1
            if sym in held:                          # 已建仓：观察完成
                retired.append(sym)
                continue
            if days > watch_days:                    # 观察期满仍未入选：出局
                retired.append(sym)
                continue
            new[sym] = {**info, "days": days}

        for pk in result.picks[:carry_top]:          # 当日精选直接续研
            if pk.symbol in held:
                continue
            old = new.get(pk.symbol, {})
            new[pk.symbol] = {
                "first_date": old.get("first_date", iso_today),
                "days": int(old.get("days", 0)),
                "last_score": round(float(pk.confidence), 4),
                "source": "pick",
            }

        for it in result.intents:                    # 高置信开仓 Intent 入池
            if it.action not in ("BUY", "ADD"):
                continue
            if float(it.confidence or 0) < min_score:
                continue
            if it.symbol in held or it.symbol in new:
                continue
            new[it.symbol] = {"first_date": iso_today, "days": 0,
                              "last_score": round(float(it.confidence), 4),
                              "source": "intent"}

        if len(new) > max_watch:                     # 容量上限：低分先出
            kept = sorted(new.items(), key=lambda kv: -float(kv[1].get("last_score", 0)))
            new = dict(kept[:max_watch])

        try:
            self.ctx.shared_repos.system.set(
                "selection:watchlist",
                json.dumps(new, ensure_ascii=False, default=str),
                reason=f"观察池 {len(new)} 只（出局 {len(retired)}）")
        except Exception as exc:                     # noqa: BLE001
            logger.warning("观察池写入失败: %s", exc)
        self.cache["watchlist"] = new
        return len(new)

    def _portfolio_snapshot(self, cs):
        from ..brain.state import PortfolioSnapshot
        ps = self.ctx.portfolio
        total = max(ps.total_asset, 1.0)
        return PortfolioSnapshot(
            total_asset=total,
            cash=ps.cash,
            position_weight={s: p.shares * p.avg_cost / total
                             for s, p in ps.positions.items()},
            industry_weight={},
            max_positions=int(self.ctx.settings.get("risk.gate1.max_positions", 10) or 10),
            max_position_pct=getattr(cs.regime, "max_position", 0.8),
            drawdown=ps.max_drawdown,
        )

    # ============================================================ 09:00 计划
    @job("plan")
    def plan(self) -> JobResult:
        """Intent → 待执行计划。这一步只落库不下单，方便盘前人工复核。

        优中选优：当日有最终精选时，开仓计划只为精选标的生成；
        其余 Intent（含对持仓的 SELL/REDUCE）照常处理。
        """
        skip = self._skip_non_trading("plan")
        if skip:
            return skip
        intents = self.cache.get("intents") or self._restore_intents()
        if not intents:
            return JobResult("plan", skipped=True, reason="无可用 Intent")

        if not self.ctx.killswitch.allow_open:
            return JobResult("plan", skipped=True,
                             reason=f"KillSwitch={self.ctx.killswitch.mode.value}，不生成开仓计划")

        pick_syms = self._pick_symbols()
        created = 0
        skipped_open = 0
        for it in intents:
            if it.action in ("HOLD",):
                continue
            is_open = it.action in ("BUY", "ADD")
            if is_open and pick_syms and it.symbol not in pick_syms:
                skipped_open += 1                    # 未入选的开仓 Intent 不进计划
                continue
            entry = float(it.entry_ref_price or 0)
            stop = self._stop_price(it, entry)
            try:
                self.ctx.repos.plans.add(
                    trade_date=self.today, symbol=it.symbol,
                    side=it.side.value if hasattr(it.side, "value") else str(it.side),
                    entry_ref_price=entry or None,
                    entry_trigger=it.entry_trigger,
                    stop_loss_price=stop or None,
                    take_profit=json.dumps([t.model_dump() for t in it.take_profit],
                                           ensure_ascii=False, default=str),
                    max_holding_days=it.max_holding_days,
                    invalidation_checks=json.dumps(it.invalidation_checks, ensure_ascii=False),
                    payload=it.model_dump_json(),
                )
                created += 1
            except Exception as exc:                 # noqa: BLE001
                logger.warning("计划落库失败 %s: %s", it.symbol, exc)
        return JobResult("plan", data={"created": created, "intents": len(intents),
                                       "skipped_open": skipped_open})

    def _pick_symbols(self) -> set[str]:
        """当日最终精选标的集。进程重启后 cache 丢失时从库里重建。"""
        picks = self.cache.get("picks")
        if picks is not None:
            return {pk.symbol for pk in picks}
        try:
            rows = self.ctx.shared_repos.picks.list_by_date(self.today)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("精选读取失败: %s", exc)
            return set()
        return {r["symbol"] for r in rows}

    def _restore_intents(self) -> list:
        """当日 Intent 列表。进程重启后内存 cache 丢失时从库里重建。

        research 的产出已全部落库（intents 表 payload=完整 JSON），
        这里反序列化回来，同一标的多次研判只保留最新一条。
        """
        from ..brain.schemas import TradeIntent
        try:
            rows = self.ctx.shared_repos.intents.list_by_date(self.today)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("Intent 读取失败: %s", exc)
            return []
        latest: dict[str, tuple[float, Any]] = {}
        for r in rows:
            payload = r.get("payload")
            if not payload:
                continue
            try:
                it = TradeIntent.model_validate_json(payload)
            except Exception:                        # noqa: BLE001
                continue
            ts = float(r.get("created_at") or 0)
            prev = latest.get(r["symbol"])
            if prev is None or ts >= prev[0]:
                latest[r["symbol"]] = (ts, it)
        intents = [it for _, it in sorted(latest.values(), key=lambda x: x[0])]
        if intents:
            self.cache["intents"] = intents          # 回填，后续任务不再查库
            logger.info("Intent 从库中重建: %d 条", len(intents))
        return intents

    @staticmethod
    def _stop_price(intent, entry: float) -> float:
        """与 ExecutionService._stop_price 保持一致的算法（P7 同路径）。"""
        if entry <= 0:
            return 0.0
        if intent.stop_loss_type == "FIXED_PCT":
            return entry * (1 - float(intent.stop_loss_value))
        if intent.stop_loss_type == "ATR":
            return entry - float(intent.stop_loss_value) * entry * 0.1
        v = float(intent.stop_loss_value or 0)
        if v > 1:                                      # 绝对价位
            return v
        return entry * (1 - v) if v > 0 else entry * 0.93

    # ============================================================ 09:20 竞价复核
    @job("auction_check")
    def auction_check(self) -> JobResult:
        """开盘前最后一道确认：停牌、一字板、跳空过大的计划直接作废。

        LLM 是昨晚/今早的判断，价格却是此刻的——两者脱节时以价格为准（P3）。
        """
        skip = self._skip_non_trading("auction_check")
        if skip:
            return skip
        pending = self.ctx.repos.plans.list_pending(self.today)
        if not pending:
            return JobResult("auction_check", skipped=True, reason="无待执行计划")

        syms = [p["symbol"] for p in pending]
        try:
            ticks = self.ctx.hub.get_realtime(syms)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("竞价快照获取失败: %s", exc)
            ticks = {}
        if not ticks:
            # 行情源整体不可用（而非个股异常）：不作废计划，交给盘中限价单兜底
            return JobResult("auction_check", skipped=True,
                             reason="竞价快照整体不可用，跳过复核（计划保留）")

        max_gap = float(self.ctx.settings.get("execution.max_auction_gap", 0.05) or 0.05)
        killed = 0
        for p in pending:
            sym = p["symbol"]
            tick = ticks.get(sym)
            ref = float(p.get("entry_ref_price") or 0)
            reason = ""
            if tick is None:
                reason = "无竞价快照"
            else:
                price = float(getattr(tick, "last", 0) or 0)
                if price <= 0:
                    reason = "疑似停牌（竞价无价）"
                elif ref > 0 and abs(price / ref - 1.0) > max_gap:
                    reason = f"跳空 {price / ref - 1:+.1%} 超过 {max_gap:.0%}"
            if reason:
                try:
                    self.ctx.repos.plans.set_status(p["id"], "CANCELLED")
                    self.ctx.repos.risk_events.add(
                        "GATE1", "AUCTION_VETO", f"{sym} {reason}",
                        symbol=sym, severity="WARN", trade_date=self.today)
                except Exception as exc:             # noqa: BLE001
                    logger.warning("作废计划失败 %s: %s", sym, exc)
                killed += 1
        return JobResult("auction_check", data={"pending": len(pending), "cancelled": killed})

    # ============================================================ ETF T+0 日内回转
    @job("etf_t0_intraday")
    def etf_t0_intraday(self) -> JobResult:
        """ETF T+0（底仓做T）盘中巡检：由 ``strategies.etf_t0.enabled`` 控制启停
        （WebUI「策略实验室」开关即运行/停止，下次触发即生效，无需重启）。

        独立于既有 intraday（主策略/策略实验室），互不干扰；仅在连续竞价时段
        且策略启用时执行，其余情况返回 skipped。业务逻辑在
        ``qmt_trade/strategies/etf_t0.py::etf_t0_tick``（本方法只做转发）。"""
        from ..strategies.etf_t0 import etf_t0_tick
        return etf_t0_tick(self)

    # ============================================================ 个股存量持仓做T
    @job("stock_t0_intraday", critical=False)
    def stock_t0_intraday(self) -> JobResult:
        """个股存量持仓做T（高抛低吸）盘中巡检：由 ``strategies.stock_t0.enabled``
        控制启停（WebUI「策略实验室」开关即运行/停止，下次触发即生效，无需重启）。

        独立于既有 intraday / ETF T+0 / 策略实验室，互不干扰：只对
        ``strategies.stock_t0.symbols`` 白名单里的**已有持仓**做日内先卖后买，
        不建仓、不净加仓、不净减仓，尾盘 T 仓强制归零。业务逻辑在
        ``qmt_trade/strategies/stock_t0.py::stock_t0_tick``（本方法只做转发）。"""
        from ..strategies.stock_t0 import stock_t0_tick
        return stock_t0_tick(self)

    # ============================================================ 09:30 盘中
    @job("intraday")
    def intraday_tick(self) -> JobResult:
        """盘中单次巡检：先守护持仓（止损优先），再执行待办计划。

        顺序不能反 —— 止损是保命的，开仓是赚钱的，先保命。
        """
        skip = self._skip_non_trading("intraday")
        if skip:
            return skip
        now = datetime.now()
        session = self.ctx.calendar.session_of(now)
        if not session.is_continuous and not self._forced_date:
            return JobResult("intraday", skipped=True, reason=f"非连续竞价时段（{session.value}）")

        ps = self.ctx.portfolio
        ps.mark_t1(self.today)
        held = list(ps.positions)
        last_prices = self._last_prices(held)
        ps.refresh(last_prices)

        # ---- Gate-2 持仓守护（含 Regime 联动的总仓位管理）----
        # 策略实验室持仓带外部止损（策略自身口径）：盘中实时按策略止损价触发，
        # 且不被主系统移动止盈/时间止损/止盈档干扰（2026-08-16 修复执行一致性）。
        ext_stops = self._lab_external_stops(ps)
        actions = self.ctx.risk.guard_positions(
            ps, last_prices=last_prices, asof=self.today, killswitch=self.ctx.killswitch,
            regime=self._regime_snapshot(), external_stops=ext_stops)
        closed = 0
        for act in actions:
            if self._execute_close(act, last_prices):
                closed += 1

        # ---- 执行待办计划 ----
        opened = 0
        if self.ctx.killswitch.allow_open:
            for p in self.ctx.repos.plans.list_pending(self.today):
                if self._execute_plan(p):
                    opened += 1

        self.ctx.persist_portfolio(self.today)
        return JobResult("intraday", data={"positions": len(ps.positions),
                                           "closed": closed, "opened": opened,
                                           "session": session.value})

    def _last_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        out: dict[str, float] = {}
        try:
            for s, t in (self.ctx.hub.get_realtime(symbols) or {}).items():
                px = float(getattr(t, "last", 0) or 0)
                if px > 0:
                    out[s] = px
        except Exception as exc:                     # noqa: BLE001
            logger.warning("实时价获取失败: %s", exc)
        # 缺价的用最近收盘价兜底：宁可用旧价触发止损，也不要漏掉止损
        missing = [s for s in symbols if s not in out]
        for s in missing:
            bar = self._bar(s)
            if bar is not None and bar.close:
                out[s] = float(bar.close)
        return out

    def _bar(self, symbol: str):
        """执行层取价必须是**真实价（不复权）**：下单/止损/账本都按真实价。
        后复权只用于因子计算，混进执行链会把复权价当真钱报出去。"""
        from ..datahub.types import Adjust
        try:
            df = self.ctx.hub.get_bars([symbol], start=self.today - timedelta(days=10),
                                       end=self.today, adjust=Adjust.NONE)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("取 bar 失败 %s: %s", symbol, exc)
            return None
        if df is None or len(df) == 0:
            return None
        row = df.iloc[-1]
        from ..datahub.types import Bar
        return Bar(symbol=symbol, date=self.today,
                   open=float(row.get("open", 0) or 0), high=float(row.get("high", 0) or 0),
                   low=float(row.get("low", 0) or 0), close=float(row.get("close", 0) or 0),
                   volume=float(row.get("volume", 0) or 0),
                   amount=float(row.get("amount", 0) or 0))

    def _submit_intent(self, intent, *, bar, plan_id: str, signal: str):
        """独立策略（策略实验室）意图提交的公共入口：走 ExecutionService 全链路。"""
        return self.ctx.execution.submit_intent(
            intent, bar=bar, market_day=self.today, asof=self.today,
            regime=self._regime_snapshot(), instrument=self._instrument(intent.symbol),
            sym_industry={}, plan_id=plan_id, signal=signal)

    def _execute_close(self, act, last_prices: dict[str, float]) -> bool:
        """平仓/减仓。守护动作走的是同一条 ExecutionService 链路（P7）。"""
        from ..brain.schemas import TradeIntent
        sym = act.symbol
        px = last_prices.get(sym) or 0.0
        if self.dry_run:
            logger.info("[dry-run] %s %s 原因=%s", act.action, sym, act.tag)
            return True
        intent = TradeIntent(
            symbol=sym, action=act.action if act.action in ("SELL", "REDUCE") else "SELL",
            confidence=1.0, conviction="HIGH",
            entry_type="LIMIT", entry_ref_price=px or None,
            stop_loss_type="FIXED_PCT", stop_loss_value=0.05,
            valid_until=self.today,
            reasoning=f"Gate-2 {act.tag}: {act.reason}",
        )
        res = self.ctx.execution.submit_intent(
            intent, bar=self._bar(sym), market_day=self.today, asof=self.today,
            regime=self._regime_snapshot(), instrument=self._instrument(sym),
            sym_industry={}, plan_id=f"guard_{self.today:%Y%m%d}", signal=act.tag,
            reduce_shares=act.shares,
            # Guard 动作每 30 秒运行一次；固定默认 seq 会让后续轮次只触发幂等键告警。
            # 使用提交时刻区分实际尝试，重复动作交给 pending/cooldown 保护。
            seq=int(datetime.now().timestamp()))
        if not res.ok:
            logger.warning("平仓失败 %s: %s(%s)", sym, res.reason, res.rejected_by)
            try:
                ev_repo = self.ctx.repos.risk_events
                # 每仓每日只记一条：盘中每 30s 巡检，T+1 不可卖等会反复触发，
                # 不去重会刷屏淹没真正重要的告警。次日解除限制后守护循环会自然重试。
                rule = f"CLOSE_FAIL:{act.tag}"
                if not ev_repo.exists(rule, symbol=sym, trade_date=self.today):
                    ev_repo.add("GATE2", rule, res.reason, symbol=sym,
                                severity="ERROR", trade_date=self.today)
            except Exception:                        # noqa: BLE001
                pass
        return res.ok

    def _execute_plan(self, plan_row: dict) -> bool:
        from ..brain.schemas import TradeIntent
        sym = plan_row["symbol"]
        payload = plan_row.get("payload")
        try:
            intent = TradeIntent.model_validate_json(payload) if payload else None
        except Exception as exc:                     # noqa: BLE001
            logger.warning("计划 payload 解析失败 %s: %s", sym, exc)
            intent = None
        if intent is None:
            self._set_plan_status(plan_row["id"], "INVALID")
            return False
        if intent.valid_until < self.today:
            self._set_plan_status(plan_row["id"], "EXPIRED")
            return False
        if self.dry_run:
            logger.info("[dry-run] 执行计划 %s %s", sym, intent.action)
            return True

        res = self.ctx.execution.submit_intent(
            intent, bar=self._bar(sym), market_day=self.today, asof=self.today,
            regime=self._regime_snapshot(), instrument=self._instrument(sym),
            sym_industry={}, plan_id=plan_row["id"], signal="PLAN")
        self._set_plan_status(plan_row["id"], "FILLED" if res.ok else "REJECTED")
        if not res.ok:
            logger.info("计划未成交 %s: %s(%s)", sym, res.reason, res.rejected_by)
        return res.ok

    def _set_plan_status(self, plan_id: str, status: str) -> None:
        try:
            self.ctx.repos.plans.set_status(plan_id, status)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("计划状态更新失败 %s→%s: %s", plan_id, status, exc)

    def _regime_snapshot(self):
        snap = self.cache.get("regime")
        if snap is not None:
            return snap
        try:
            snap = self.ctx.pipeline.detector.detect(self.today)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("Regime 判定失败，按 RANGE 保守处理: %s", exc)
            from ..features.regime import Regime, RegimeSnapshot
            snap = RegimeSnapshot(asof=self.today, regime=Regime.RANGE,
                                  max_position=0.5, min_score=0.0, degraded=True,
                                  reason="Regime 判定异常，降级 RANGE")
        self.cache["regime"] = snap
        return snap

    def _instrument(self, symbol: str):
        try:
            return self.ctx.hub.get_instrument(symbol)
        except Exception:                            # noqa: BLE001
            return None

    # ============================================================ 尾盘选股法（独立策略，默认关闭）
    def _tail_pick_cfg(self):
        # 实时读配置（绕过 ctx 缓存的单例），使 strategies.tail_pick.enabled
        # 在 UI 勾选保存后、下一次触发即生效，无需重启后端。
        from ..core.config import get_settings
        from ..strategies.tail_pick import TailPickConfig
        return TailPickConfig.from_settings(get_settings())

    def _tail_pick_bought_yesterday(self) -> list[str]:
        """读取昨日 14:30 买入的尾盘标的集合（库读，重启可重建）。"""
        prev = self._prev_trading_day(self.today)
        if prev is None:
            return []
        try:
            raw = self.ctx.shared_repos.system.get(f"tail_pick:bought:{prev.isoformat()}")
            return json.loads(raw) if raw else []
        except Exception:                            # noqa: BLE001
            return []

    @job("tail_pick_select", critical=False)
    def tail_pick_select(self) -> JobResult:
        """14:30 尾盘选股（独立策略）。``enabled=false`` 时跳过。

        通过 8 层筛选后：候选存入 system_state 供 UI/退出任务使用；
        若开启自动交易（enabled），经 ExecutionService 提交买入。
        选股层完全独立，不复用现有 SelectionPipeline/Regime。
        """
        cfg = self._tail_pick_cfg()
        if not cfg.enabled:
            return JobResult("tail_pick_select", skipped=True,
                             reason="strategies.tail_pick.enabled=false（默认关闭）")
        skip = self._skip_non_trading("tail_pick_select")
        if skip:
            return skip

        from ..strategies.tail_pick import TailPickStrategy
        strat = TailPickStrategy(self.ctx.settings, self.ctx.hub)
        # 真数据模式下要求分钟线，避免日线降级产生不可靠结论
        minute = True
        picks = strat.select(self.today, minute_available=minute)
        self.cache["tail_pick_picks"] = picks
        try:
            payload = [{"symbol": p.symbol, "entry_price": p.entry_price,
                        "reasons": p.reasons, "minute_verified": p.minute_verified}
                       for p in picks]
            self.ctx.shared_repos.system.set(
                "selection:tail_pick:latest",
                json.dumps(payload, ensure_ascii=False, default=str),
                reason=f"{len(picks)} 只尾盘候选")
        except Exception as exc:                     # noqa: BLE001
            logger.warning("尾盘候选落库失败: %s", exc)

        opened = 0
        if picks and self.ctx.killswitch.allow_open:
            intents = strat.to_intents(picks, valid_until=self.today)
            for it in intents:
                res = self.ctx.execution.submit_intent(
                    it, bar=self._bar(it.symbol), market_day=self.today, asof=self.today,
                    regime=self._regime_snapshot(), instrument=self._instrument(it.symbol),
                    sym_industry={}, plan_id=f"tp_sel_{self.today:%Y%m%d}", signal="TAIL_PICK")
                if res.ok:
                    opened += 1
            try:
                self.ctx.shared_repos.system.set(
                    f"tail_pick:bought:{self.today.isoformat()}",
                    json.dumps([p.symbol for p in picks], ensure_ascii=False))
            except Exception:                         # noqa: BLE001
                pass
        return JobResult("tail_pick_select",
                         data={"picks": len(picks), "opened": opened,
                               "minute_verified": (picks[0].minute_verified if picks else None)})

    @job("tail_pick_exit", critical=False)
    def tail_pick_exit(self) -> JobResult:
        """09:35 隔夜仓离场（独立策略）。``enabled=false`` 时跳过。

        卖出昨日 14:30 买入的尾盘标的，遵守一夜持股纪律（T+1 开盘 30min 内离场）。
        隔夜硬止损由 ExecutionService 在开盘触及止损价时自动成交。
        """
        cfg = self._tail_pick_cfg()
        if not cfg.enabled:
            return JobResult("tail_pick_exit", skipped=True,
                             reason="strategies.tail_pick.enabled=false（默认关闭）")
        skip = self._skip_non_trading("tail_pick_exit")
        if skip:
            return skip

        syms = self.cache.get("tail_pick_bought") or self._tail_pick_bought_yesterday()
        closed = 0
        from ..brain.schemas import TradeIntent
        for sym in syms:
            pos = self.ctx.portfolio.positions.get(sym)
            if pos is None or pos.shares <= 0:
                continue
            it = TradeIntent(
                symbol=sym, action="SELL", confidence=1.0, conviction="HIGH",
                entry_type="MARKET", entry_ref_price=None,
                stop_loss_type="FIXED_PCT", stop_loss_value=0.0,
                max_weight_hint=0.3, max_holding_days=1, valid_until=self.today,
                reasoning="尾盘选股法隔夜离场（开盘30min内）")
            res = self.ctx.execution.submit_intent(
                it, bar=self._bar(sym), market_day=self.today, asof=self.today,
                regime=self._regime_snapshot(), instrument=self._instrument(sym),
                sym_industry={}, plan_id=f"tp_exit_{self.today:%Y%m%d}", signal="TAIL_PICK_EXIT")
            if res.ok:
                closed += 1
        return JobResult("tail_pick_exit", data={"targets": len(syms), "closed": closed})

    # ============================================================ 策略实验室（独立策略，UI 可启停）
    def _slab_run_all(self, phase: str) -> dict:
        """对启用中的策略实验室策略执行对应相位（open=开盘买 / close=尾盘买+持仓管理）。"""
        from ..strategies.live import LabLiveRunner
        summary: dict[str, dict] = {}
        for sid in ("limit_up", "second_board", "dip_buy", "trend_buy"):
            try:
                summary[sid] = LabLiveRunner(self, sid).daily(phase)
            except Exception as exc:                 # noqa: BLE001
                logger.error("strategylab %s %s 异常: %s", sid, phase, exc)
                summary[sid] = {"sid": sid, "error": str(exc)}
        return summary

    @job("strategylab_open", critical=False)
    def strategylab_open(self) -> JobResult:
        """09:35 开盘相位：打板/二板开盘买入（T-1 选池 + 开盘涨幅过滤）。"""
        skip = self._skip_non_trading("strategylab_open")
        if skip:
            return skip
        summary = self._slab_run_all("open")
        opened = sum(s.get("opened", 0) for s in summary.values())
        return JobResult("strategylab_open",
                         data={"summary": summary, "opened": opened})

    @job("strategylab_run", critical=False)
    def strategylab_run(self) -> JobResult:
        """14:45 收盘相位：全部 lab 策略持仓管理 + 尾盘低吸/趋势买点尾盘买入
        + 趋势买点日收益入策略池。"""
        skip = self._skip_non_trading("strategylab_run")
        if skip:
            return skip
        summary = self._slab_run_all("close")
        managed = sum(s.get("managed", 0) for s in summary.values())
        opened = sum(s.get("opened", 0) for s in summary.values())
        return JobResult("strategylab_run",
                         data={"summary": summary, "managed": managed, "opened": opened})

    def _lab_external_stops(self, ps=None) -> dict[str, float]:
        """策略实验室持仓的外部止损价 {symbol: 绝对价}（system_state 元数据）。

        止损价 = 当前成本 ×(1−stop_pct)（随除权/成本调整自适应；元数据同时存
        stop_pct 与绝对 stop_price，绝对价仅作兜底）。供 Gate-2 盘中精确止损，
        与回测"破位止损"口径一致。
        """
        out: dict[str, float] = {}
        try:
            keys = self.ctx.shared_repos.system.list_keys("strategylab:meta:")
        except Exception as exc:                     # noqa: BLE001
            logger.warning("读取 lab 止损元数据失败: %s", exc)
            return out
        for k in keys or []:
            sym = str(k).rsplit(":", 1)[-1]
            raw = self.ctx.shared_repos.system.get(k)
            if not raw:
                continue
            try:
                meta = json.loads(raw)
                stop_pct = float(meta.get("stop_pct") or 0)
                if stop_pct <= 0:
                    continue
                pos = (ps.positions.get(sym) if ps is not None else None) \
                    or self.ctx.portfolio.positions.get(sym)
                avg_cost = float(getattr(pos, "avg_cost", 0) or 0) if pos else 0.0
                if avg_cost > 0:
                    out[sym] = avg_cost * (1 - stop_pct)
                else:
                    stop_abs = float(meta.get("stop_price") or 0)
                    if stop_abs > 0:
                        out[sym] = stop_abs
            except Exception:                        # noqa: BLE001
                continue
        return out

    # ============================================================ 15:05 对账
    @job("reconcile")
    def reconcile(self) -> JobResult:
        """Gate-3 盘后对账。只有实盘才有券商可对；模拟盘直接跳过。"""
        skip = self._skip_non_trading("reconcile")
        if skip:
            return skip
        broker = self.ctx.gateway
        if not all(hasattr(broker, m) for m in
                   ("query_positions", "query_asset", "query_trades")):
            return JobResult("reconcile", skipped=True,
                             reason=f"{self.ctx.mode} 模式无券商可对账")

        self.ctx.persist_portfolio(self.today)       # 先把内存账本刷进库再比
        res = self.ctx.reconciler.run(self.today, broker)
        why = res.error or "; ".join(d.render() for d in res.blocking[:3])
        return JobResult("reconcile", ok=res.passed,
                         reason="" if res.passed else (why or "存在阻断级差异"),
                         data={"discrepancies": len(res.discrepancies),
                               "blocking": len(res.blocking)})

    # ============================================================ 16:00 复盘
    @job("review", critical=False)
    def review(self) -> JobResult:
        skip = self._skip_non_trading("review")
        if skip:
            return skip
        ps = self.ctx.portfolio
        ps.record_equity(day_end=True)
        self.ctx.persist_portfolio(self.today)

        from ..evolution.review import ReviewEngine
        engine = ReviewEngine(self.ctx.settings)
        realized = list(ps.closed_trades)
        if not realized:                             # 进程重启过就从库里捞
            realized = [t for t in self.ctx.repos.trades.list_by_date(self.today)
                        if t.get("realized_pnl") is not None]
        evo_cfg = self.ctx.settings.section("evolution") or {}
        fwd_days = int(evo_cfg.get("ic_forward_days", 5))
        frame, fwd = self._factor_ic_inputs(fwd_days)
        rev = engine.run(self.today, realized, factor_frame=frame, forward_returns=fwd)
        self._record_selection_hit(frame, fwd, self.today - timedelta(days=fwd_days),
                                   top_k=int(evo_cfg.get("hit_top_k", 5)))
        for les in rev.lessons:
            try:
                self.ctx.shared_repos.experiences.add(
                    trade_date=self.today, situation=les.message,
                    lesson=les.suggestion or les.message,
                    tags=f"{les.tag}|{les.severity}",
                    outcome=json.dumps(les.evidence, ensure_ascii=False, default=str))
            except Exception as exc:                 # noqa: BLE001
                logger.warning("经验落库失败: %s", exc)
        if rev.factor_ic:
            try:
                self.ctx.shared_repos.system.set(
                    f"evolution:factor_ic:{self.today.isoformat()}",
                    json.dumps(rev.factor_ic),
                    reason=f"{len(rev.factor_ic)} 个因子 IC")
            except Exception as exc:                 # noqa: BLE001
                logger.warning("因子 IC 落库失败: %s", exc)

        # 策略池记账：当日组合收益喂给周末调权/影子转正（L5 进化闭环）
        curve = list(ps.equity_curve)
        if len(curve) >= 2 and curve[-2] > 0:
            try:
                self.ctx.pool.record("main", curve[-1] / curve[-2] - 1.0, self.today)
                self.ctx.save_pool()
            except Exception as exc:                 # noqa: BLE001
                logger.warning("策略池记账失败: %s", exc)

        health = self.ctx.monitor.check(self.today, notify=False)
        rep = self.ctx.reporter.daily(self.today, health=health.to_dict(),
                                      lessons=[les.render() for les in rev.lessons])
        path = None
        try:
            path = self.ctx.reporter.save(rep)
            self.ctx.reporter.push(rep)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("日报输出失败: %s", exc)

        # 盘后自我复盘与总结：反思选股/交易、提出改进策略、沉淀短期/长期记忆（L5 闭环）。
        reflect = self._self_reflect(self.today, rev)

        self.ctx.guard.on_new_day()                  # 清当日下单计数，为明天做准备
        return JobResult("review", data={"lessons": len(rev.lessons),
                                         "trades": len(realized),
                                         "factor_ic": len(rev.factor_ic),
                                         "healthy": health.healthy,
                                         "report": str(path) if path else "",
                                         "reflection": str(reflect["path"]) if reflect else "",
                                         "long_term": reflect["long_term"] if reflect else 0})

    # ============================================================ 盘后自我复盘
    def _self_reflect(self, d: date, rev) -> dict | None:
        """生成盘后自我复盘（reflection_YYYYMMDD.md）+ 短期/长期记忆落库。

        失败不影响复盘主流程（P4）：LLM/IO 异常都被接住，最多只是少了这份总结。
        返回 {path, long_term} 供 JobResult 记录；异常返回 None。
        """
        try:
            from ..evolution.reflection import (
                ReflectionEngine, load_long_term, dump_long_term)
            eng = ReflectionEngine(self.ctx.settings)

            # ---- 采集上下文（全部库读，失败即退化为空）----
            picks: list[dict] = []
            try:
                picks = self.ctx.shared_repos.picks.list_by_date(d)
            except Exception as exc:                 # noqa: BLE001
                logger.warning("复盘采集 picks 失败: %s", exc)
            trades: list[dict] = []
            try:
                trades = self.ctx.repos.trades.list_by_date(d)
            except Exception as exc:                 # noqa: BLE001
                logger.warning("复盘采集 trades 失败: %s", exc)

            regime = ""
            try:
                raw = self.ctx.shared_repos.system.get("regime:latest")
                regime = (json.loads(raw) or {}).get("regime", "") if raw else ""
            except Exception:                        # noqa: BLE001
                pass

            factor_ic: dict[str, float] = {}
            try:
                raw = self.ctx.shared_repos.system.get(f"evolution:factor_ic:{d.isoformat()}")
                factor_ic = json.loads(raw) if raw else {}
            except Exception:                        # noqa: BLE001
                pass

            selection_hit: dict[str, float] | None = None
            try:
                keys = self.ctx.shared_repos.system.list_keys("selection:hit:")[-5:]
                hs = []
                for k in keys:
                    try:
                        h = json.loads(self.ctx.shared_repos.system.get(k) or "{}")
                        if h:
                            hs.append(h)
                    except Exception:                # noqa: BLE001
                        continue
                if hs:
                    n = len(hs)
                    selection_hit = {
                        "eval_days": float(n),
                        "hit_days": float(sum(1 for h in hs if h.get("hit"))),
                        "top_avg": sum(float(h.get("top_avg", 0)) for h in hs) / n,
                        "all_avg": sum(float(h.get("all_avg", 0)) for h in hs) / n,
                    }
            except Exception:                        # noqa: BLE001
                pass

            recent_exp: list[str] = []
            try:
                rows = self.ctx.shared_repos.experiences.recent(
                    d - timedelta(days=10), limit=12, tags_like="WARN")
                recent_exp = [str(r.get("lesson") or r.get("situation") or "").strip()
                              for r in rows if (r.get("lesson") or r.get("situation"))]
            except Exception:                        # noqa: BLE001
                pass

            prev_st = self._read_short_term(self._prev_trading_day(d))
            long_term_prev = load_long_term(
                self.ctx.shared_repos.system.get("reflection:long_term"))

            # ---- LLM 增强（显式开启且可用才传；否则纯规则，零成本）----
            llm = None
            if eng.llm_enabled:
                client = getattr(self.ctx.brain, "client", None)
                if client is not None and getattr(client, "enabled", False):
                    llm = client

            rep = eng.run(d, review_result=rev, picks=picks, trades=trades,
                          regime=regime, factor_ic=factor_ic, selection_hit=selection_hit,
                          recent_experiences=recent_exp, short_term_prev=prev_st,
                          long_term_prev=long_term_prev, llm=llm)

            # ---- 落盘 reflection_YYYYMMDD.md ----
            out_dir = self.ctx.reporter.output_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"reflection_{d:%Y%m%d}.md"
            path.write_text(rep.to_markdown(), encoding="utf-8")

            # ---- 记忆落库 ----
            try:
                self.ctx.shared_repos.system.set(
                    f"reflection:short_term:{d.isoformat()}",
                    json.dumps(rep.short_term, ensure_ascii=False),
                    reason="短期记忆（明日待办）")
                self.ctx.shared_repos.system.set(
                    "reflection:long_term", dump_long_term(rep.long_term),
                    reason=f"长期记忆（{len(rep.long_term)} 条）")
            except Exception as exc:                 # noqa: BLE001
                logger.warning("复盘记忆落库失败: %s", exc)

            logger.info("盘后自我复盘已生成 %s（LLM=%s，长期记忆 %d 条）",
                        path, rep.llm_used, len(rep.long_term))
            return {"path": path, "long_term": len(rep.long_term)}
        except Exception as exc:                     # noqa: BLE001
            logger.warning("盘后自我复盘生成失败（不影响主流程）: %s", exc)
            return None

    def _prev_trading_day(self, d: date) -> date | None:
        """向前找最近一个交易日（用于读取『昨日短期记忆』做延续）。"""
        cur = d - timedelta(days=1)
        for _ in range(14):
            try:
                if self.ctx.calendar.is_trading_day(cur):
                    return cur
            except Exception:                        # noqa: BLE001
                return None
            cur -= timedelta(days=1)
        return None

    def _read_short_term(self, d: date | None) -> list[str]:
        if d is None:
            return []
        try:
            raw = self.ctx.shared_repos.system.get(f"reflection:short_term:{d.isoformat()}")
            items = json.loads(raw) if raw else []
            return [str(x) for x in items] if isinstance(items, list) else []
        except Exception:                            # noqa: BLE001
            return []

    def _factor_ic_inputs(self, fwd_days: int):
        """组装复盘算 IC 的输入：N 天前的因子截面 + 截至今日的前向收益。

        IC 必须滞后评估（选股日看不到后 N 天的收益），所以读的是
        ``today - fwd_days`` 那天落库的截面。任何一环缺数据都返回空，
        复盘降级为只做逐笔归因。
        """
        import pandas as pd
        from ..datahub.types import Adjust, Freq
        try:
            base_day = self.today - timedelta(days=fwd_days)
            raw = self.ctx.shared_repos.system.get(f"selection:frame:{base_day.isoformat()}")
            if not raw:
                return None, None
            frame = pd.DataFrame(json.loads(raw))
            if frame.empty or "symbol" not in frame.columns:
                return None, None
            syms = frame["symbol"].astype(str).tolist()
            bars = self.ctx.hub.get_bars(syms, Freq.D1, base_day, self.today, Adjust.HFQ)
            if bars is None or bars.empty:
                return None, None
            fwd: dict[str, float] = {}
            for s, sub in bars.groupby("symbol"):
                if len(sub) >= 2 and float(sub["close"].iloc[0]) > 0:
                    fwd[str(s)] = float(sub["close"].iloc[-1] / sub["close"].iloc[0] - 1.0)
            return frame, (fwd or None)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("因子 IC 输入组装失败: %s", exc)
            return None, None

    def _record_selection_hit(self, frame, fwd, base_day, *, top_k: int = 5) -> None:
        """选股质量跟踪：N 天前推荐的 Top-K 标的，前向收益是否跑赢截面均值。

        命中与否只看相对表现（top_avg > all_avg）而非绝对涨跌——
        熊市里全军覆没但选得比别人抗跌，选股逻辑仍然是有效的。
        结果按选股日落库 ``selection:hit:{base_day}``，供阶段报告聚合。
        """
        try:
            if frame is None or not fwd or "score" not in getattr(frame, "columns", ()):
                return
            scored = [(str(sym), float(sc)) for sym, sc
                      in zip(frame["symbol"], frame["score"]) if str(sym) in fwd]
            if len(scored) < 3:
                return
            scored.sort(key=lambda x: -x[1])
            tops = [fwd[s] for s, _ in scored[:top_k]]
            alls = [fwd[s] for s, _ in scored]
            top_avg = sum(tops) / len(tops)
            all_avg = sum(alls) / len(alls)
            self.ctx.repos.system.set(
                f"selection:hit:{base_day.isoformat()}",
                json.dumps({"top_k": len(tops), "top_avg": round(top_avg, 6),
                            "all_avg": round(all_avg, 6), "hit": bool(top_avg > all_avg)},
                           ensure_ascii=False),
                reason=f"Top{len(tops)} 前向 {top_avg:+.2%} vs 截面 {all_avg:+.2%}")
        except Exception as exc:                     # noqa: BLE001
            logger.warning("选股命中率落库失败: %s", exc)

    # ============================================================ 周日 进化
    @job("evolve", critical=False)
    def evolve(self) -> JobResult:
        """周度进化：策略池调权 + 周报。参数寻优很重，单独由 CLI 手动触发。"""
        pool = self.ctx.pool
        rb = pool.rebalance(self.today)
        self.ctx.save_pool()

        rep = self.ctx.reporter.weekly(self.today, pool_weights=rb.weights)
        path = None
        try:
            path = self.ctx.reporter.save(rep)
            self.ctx.reporter.push(rep)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("周报输出失败: %s", exc)

        # 阶段分析报告：按阶段汇总每日绩效/收益 + 选股命中率 + 因子 IC 趋势
        ops_cfg = self.ctx.settings.section("ops") or {}
        rep_cfg = ops_cfg.get("report", {}) if isinstance(ops_cfg.get("report"), dict) else {}
        stage_days = int(rep_cfg.get("stage_days", 30))
        stage_path = None
        try:
            srep = self.ctx.reporter.stage(self.today, days=stage_days)
            stage_path = self.ctx.reporter.save(srep)
            self.ctx.reporter.push(srep)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("阶段报告输出失败: %s", exc)
        return JobResult("evolve", data={"changed": sum(1 for d in rb.decisions if d.changed),
                                         "weights": rb.weights,
                                         "report": str(path) if path else "",
                                         "stage_report": str(stage_path) if stage_path else ""})

    # ============================================================ 随时 健康检查
    @job("health", critical=False)
    def health(self) -> JobResult:
        rep = self.ctx.monitor.check(self.today)
        return JobResult("health", ok=rep.healthy,
                         reason="" if rep.healthy else
                                "; ".join(c.message for c in rep.failed[:3]),
                         data={"checks": len(rep.results),
                               "failed": len(rep.failed),
                               "degraded": rep.degraded,
                               "kill_mode": self.ctx.killswitch.mode.value})

    # ============================================================ 编排
    def run_morning(self) -> list[JobResult]:
        """盘前全流程。任何一步 fail 都继续往下走——
        后面的任务自己会判断前置条件是否满足（比如没有候选池就跳过研判）。"""
        return [self.data_sync(), self.regime(), self.selection(),
                self.research(), self.plan(), self.auction_check()]

    def run_evening(self) -> list[JobResult]:
        return [self.reconcile(), self.review()]

    def run_once_all(self) -> list[JobResult]:
        """一次性跑完一整天（回放/联调用）。"""
        out = self.run_morning()
        out.append(self.intraday_tick())
        out.extend(self.run_evening())
        return out

    def summary(self) -> str:
        lines = [r.render() for r in self.history]
        ok = sum(1 for r in self.history if r.ok and not r.skipped)
        fail = sum(1 for r in self.history if not r.ok)
        skip = sum(1 for r in self.history if r.skipped)
        lines.append(f"—— 共 {len(self.history)} 项：成功 {ok} / 失败 {fail} / 跳过 {skip}")
        return "\n".join(lines)


#: 供 CLI ``--once`` 使用的任务名 → 方法名映射
JOB_MAP: dict[str, str] = {
    "data_sync": "data_sync",
    "regime": "regime",
    "selection": "selection",
    "research": "research",
    "plan": "plan",
    "auction_check": "auction_check",
    "intraday": "intraday_tick",
    "reconcile": "reconcile",
    "review": "review",
    "evolve": "evolve",
    "health": "health",
    "tail_pick_select": "tail_pick_select",
    "tail_pick_exit": "tail_pick_exit",
    "strategylab_open": "strategylab_open",
    "strategylab_run": "strategylab_run",
    "etf_t0_intraday": "etf_t0_intraday",
    "stock_t0_intraday": "stock_t0_intraday",
}


def run_job(runner: JobRunner, name: str) -> JobResult:
    """按名字执行单个任务。名字非法时返回失败结果而非抛异常。"""
    method = JOB_MAP.get(name)
    if method is None:
        return JobResult(name, ok=False,
                         reason=f"未知任务，可选：{', '.join(sorted(JOB_MAP))}")
    return getattr(runner, method)()


__all__ = ["JobResult", "JobRunner", "JOB_MAP", "run_job", "job", "CRITICAL_JOBS"]
