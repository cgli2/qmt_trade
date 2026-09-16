"""DataHub —— 统一数据接口 + 优先级降级 + PIT + 质量校验 + 缓存。

对应设计 6.1。三个关键点：

1. **所有接口都有 ``asof``**，PIT 在这一层统一施加，下游模块不需要自己操心穿越问题；
2. **降级不是无脑重试**：熔断打开的源直接跳过，不浪费一次超时；
3. **全部源失败抛 DataUnavailableError**，由调度层转为「当日停止开仓」（P4）。
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd

from ..core.clock import Session, TradingCalendar
from ..core.config import Settings
from ..core.errors import DataQualityError, DataUnavailableError
from ..core.instruments import build_profile, normalize_symbol
from ..core.logging import get_logger
from .cache import CategoryCache
from .pit import PITGuard
from .providers.base import Capability, DataProvider
from .sentiment import score_sentiment
from .store import DuckDBStore
from .types import Adjust, CorpEvent, Freq, Fundamental, InstrumentInfo, NewsItem, Tick, SourceSkipped

logger = get_logger("datahub.manager")


def _sym_list(symbols) -> list[str] | None:
    """统一入参：允许传单个字符串、序列或 None。"""
    if symbols is None:
        return None
    if isinstance(symbols, str):
        symbols = [symbols]
    return [normalize_symbol(s) for s in symbols]


def _tail_fingerprint(df: pd.DataFrame, since: date) -> tuple[int, float, float]:
    """``since`` 起（含）所有行的廉价指纹：(行数, close 之和, volume 之和)。

    用于判断一次增量探测是否**真的带回了新数据**。日线探测在夜间/停牌/休市时
    会反复返回同一根 bar，若无此判定就会每分钟重写一次 parquet + DuckDB，
    与实盘进程争单写者锁。指纹只取 close/volume：盘中当日 bar 一旦变化，
    成交量必然增长，足以判据；比逐格 DataFrame.equals 便宜且不依赖列序/dtype。
    """
    if df is None or df.empty:
        return (0, 0.0, 0.0)
    sub = df.loc[pd.to_datetime(df["date"]).dt.date >= since]
    if sub.empty:
        return (0, 0.0, 0.0)
    return (len(sub),
            round(float(sub["close"].sum()), 6),
            round(float(sub["volume"].sum()), 3))


class QualityReport:
    """行情体检结论。**阻断性问题与良性提示分开装**，这是本类的全部要点。

    - ``issues``：真会让下游算错的东西 → ``ok=False``。``data_sync`` 是 CRITICAL
      任务，``not ok`` 会把整个系统降级到 REDUCE_ONLY，所以进这个列表的门槛必须高。
    - ``notes``：**合法但看起来异常**的行情（停牌日 OHLC 记 0、新股上市初期不设
      涨跌幅限制）→ 不影响 ``ok``，只留痕供排查。

    旧实现只有 ``issues``，把停牌股的 ``low=0`` 也算脏数据 —— 一只停牌股就能拉闸
    全系统，且每 30 秒的盘中巡检都会在日志里刷一条同样的 WARNING。
    """

    def __init__(self):
        self.issues: list[str] = []
        self.notes: list[str] = []

    def add(self, msg: str) -> None:
        self.issues.append(msg)

    def note(self, msg: str) -> None:
        """记一条不影响 ``ok`` 的良性提示。"""
        self.notes.append(msg)

    @property
    def ok(self) -> bool:
        return not self.issues

    def __str__(self) -> str:  # pragma: no cover
        parts = []
        if self.issues:
            parts.append("; ".join(self.issues))
        if self.notes:
            parts.append("[提示] " + "; ".join(self.notes))
        return " | ".join(parts) if parts else "OK"


#: 质量告警的限流窗口（秒）。``get_bars`` 是热路径——盘中巡检 30 秒一 tick，
#: 每 tick 都会对同一份缓存帧重跑一遍校验，不限流就是每 30 秒刷一条同样的 WARNING。
QUALITY_WARN_INTERVAL = 600.0

#: ``签名 -> (上次打印时刻, 期间被抑制次数)``
_quality_warn_state: dict[str, tuple[float, int]] = {}


def _bar_samples(df: pd.DataFrame, mask, n: int = 3) -> str:
    """把命中行压成 ``600000.SH@2026-09-10`` 样本串，让告警能直接定位到标的和日期。

    旧报告只写「low 存在非正价格」——不给条数、不给标的，运维拿到这行字什么也做不了，
    既无法判断严重性也无法复核，最后只能当噪音忽略。
    """
    hit = df[mask]
    if hit.empty:
        return ""
    out = []
    for _, row in hit.head(n).iterrows():
        d = row.get("date", "")
        try:
            d = pd.Timestamp(d).strftime("%Y-%m-%d")
        except Exception:                            # noqa: BLE001
            d = str(d)[:10]
        out.append(f"{row.get('symbol', '?')}@{d}")
    more = len(hit) - len(out)
    return ", ".join(out) + (f" 等 {more} 处" if more > 0 else "")


def warn_quality(report: QualityReport, *, interval: float | None = None) -> None:
    """按**问题种类**去重限流后打印质量报告。

    签名里剔掉数字：条数从 118 变 120 不算新问题，否则限流形同虚设。
    纯 ``notes``（停牌/新股这类合法极端值）降到 DEBUG——它们本来就不该占用运维视野。
    """
    if not report.issues:
        if report.notes:
            logger.debug("行情数据提示: %s", report)
        return
    sig = "|".join(sorted({re.sub(r"\d+", "#", m) for m in report.issues}))
    wait = QUALITY_WARN_INTERVAL if interval is None else float(interval)
    now = time.time()
    last, suppressed = _quality_warn_state.get(sig, (0.0, 0))
    if now - last < wait:
        _quality_warn_state[sig] = (last, suppressed + 1)
        return
    tail = f"（期间另有 {suppressed} 次同类告警被抑制）" if suppressed else ""
    _quality_warn_state[sig] = (now, 0)
    logger.warning("行情数据质量问题: %s%s", report, tail)


class DataHub:
    """数据总线。

    ``asof`` 语义：
    - ``None``  → 实盘模式，不做时间切片（拿到什么就是什么）
    - 日期/时刻 → 回测或复现模式，晚于该时刻的数据一律不可见
    """

    def __init__(
        self,
        settings: Settings,
        providers: Sequence[DataProvider] | None = None,
        *,
        store: DuckDBStore | None = None,
        asof: date | datetime | None = None,
        strict_pit: bool = True,
    ):
        self.settings = settings
        self.providers: dict[str, DataProvider] = {}
        self.priority: dict[str, list[str]] = {
            k: list(v) for k, v in settings.section("datahub.priority").items()
        }
        cb = settings.section("datahub.circuit_breaker")
        self.fail_threshold = int(cb.get("fail_threshold", 3))
        self.cooldown = float(cb.get("cooldown_seconds", 300))
        cache_cfg = settings.section("datahub.cache")
        self.cache = CategoryCache(
            max_items=int(cache_cfg.get("max_items", 4096)),
            ttl_overrides={
                "minute_bar": cache_cfg.get("minute_bar_ttl", 60),
                "daily_bar": cache_cfg.get("daily_bar_ttl", 86400),
                "fundamental": cache_cfg.get("fundamental_ttl", 86400),
            },
        )
        self.store = store or DuckDBStore(settings.data_dir / "parquet")
        # 行情磁盘持久化缓存目录（性能修复 2026-08-12）：
        # 历史日线"拉一次落盘，重跑直接读"，避免每次回测都从数据源重拉全量。
        self.bars_cache_dir = settings.data_dir / "bars_cache"
        try:
            self.bars_cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.bars_cache_dir = None
        # 过期日线缓存清理节流（b2）：写路径触发，每进程每小时至多一次。
        self._last_bars_evict = 0.0
        self.asof = asof
        self.strict_pit = strict_pit
        quality = settings.section("datahub.quality")
        self.max_missing_ratio = float(quality.get("max_missing_ratio", 0.2))
        self.max_abs_return = float(quality.get("max_abs_return", 0.35))
        # 上市 <= 该天数的标的视为「新股期」：主板首日 +44% 无涨停板约束、
        # 创业板/科创板/北交所前 5 日完全不设涨跌幅限制，单日 |涨跌幅| > 35% 是
        # 合法行情。设 0 关闭豁免。
        self.new_listing_grace_days = int(quality.get("new_listing_grace_days", 10))
        self.quality_warn_interval = float(quality.get("warn_interval", QUALITY_WARN_INTERVAL))
        self._instrument_cache: dict[str, InstrumentInfo] = {}
        self._instrument_missing: set[str] = set()
        # 全量标的池(universe)TTL 缓存：/market/symbols 等每次点 tab 都会空参请求全市场
        # 标的（akshare 全量快照 / QMT 逐只 detail，1~5s+），而标的名单日内极稳定。
        # 仅实盘模式(asof is None)缓存；回测(asof 有值)必须走 PIT，绝不复用，避免前视偏差。
        self._universe_cache: list[InstrumentInfo] | None = None
        self._universe_cache_at: float = 0.0
        self.universe_ttl = float(cache_cfg.get("universe_ttl", 1800))
        # ---- 增量补齐探测的负缓存（性能修复 2026-09-16）----
        # 请求 end=今天而缓存只覆盖到上一交易日时，缺口探测可能**注定拿不到数据**
        # （盘前/非交易日由 _clamp_probe_end 直接收敛掉；收盘后数据源尚未发布当日
        # 日线、竞价窗口、意外休市等日历推不出来的情形则落到这里），却要沿
        # qmt→akshare 降级链空跑一轮（实测 ~6s/次，主体是 akshare 全量 HTTP 拉取），
        # 且失败结果不被记忆 → K线图页面每点一只个股就重付一次 6s。
        # 这里按 (freq, adjust, probe_start, probe_end) 记录探测失败时刻，
        # 冷却窗内跳过探测、沿用缓存。key **不含 symbol**：失败根因是"数据源在该
        # 日期区间尚无数据"，与具体标的无关，一次失败即可惠及全部标的。
        # 只在**失败**时写入，探测成功即清除；代价是新数据最多延迟一个冷却窗可见。
        self._delta_probe_fail_at: dict[tuple, float] = {}
        self.delta_probe_cooldown = float(cache_cfg.get("delta_probe_cooldown", 60))
        # ---- 盘中「当日 bar」定期刷新（数据正确性修复 2026-09-16）----
        # 日线范围感知缓存 key **不含 end**，内存 TTL 24h、磁盘 TTL 12h。盘中缓存
        # 一旦写入当日 bar，增量分支前置条件 cached_end < end 恒为假 → 永不重探，
        # 当日那根 bar 被**冻结在首次取数的时刻**（实测 14:39 的 K 线仍显示 09:40
        # 的 close=21.23 / vol=1300 手，而实时价已是 21.54 / vol=15328 手，
        # 偏差 1.4%，MA5/10/20/60 全部跟着错）。故按此 TTL 主动重探当日 bar。
        # <=0 关闭刷新（退回"拉一次用一天"的旧行为）。
        self.today_bar_refresh_ttl = float(cache_cfg.get("today_bar_refresh_ttl", 60))
        self._today_refresh_at: dict[tuple, float] = {}
        # 交易日历：把增量探测上界收敛到「最近一个可能已有数据的交易日」。
        self._cal = TradingCalendar()

        for p in providers or ():
            self.register(p)

    # ------------------------------------------------------------ 注册与选源
    def register(self, provider: DataProvider) -> None:
        provider.health.fail_threshold = self.fail_threshold
        provider.health.cooldown_seconds = self.cooldown
        self.providers[provider.name] = provider

    def set_asof(self, asof: date | datetime | None) -> None:
        self.asof = asof

    @property
    def guard(self) -> PITGuard:
        return PITGuard(self.asof, strict=self.strict_pit)

    def _candidates(self, category: str, cap: Capability) -> list[DataProvider]:
        names = self.priority.get(category) or list(self.providers)
        ordered: list[DataProvider] = []
        for name in names:
            p = self.providers.get(name)
            if p is None or not p.supports(cap):
                continue
            ordered.append(p)
        # 优先级表里没列到、但支持该能力的源作为兜底追加
        for name, p in self.providers.items():
            if p.supports(cap) and p not in ordered:
                ordered.append(p)
        return ordered

    def _dispatch(
        self,
        category: str,
        cap: Capability,
        fn: Callable[[DataProvider], Any],
        *,
        allow_empty: bool = False,
        context: str = "",
        empty_is_fault: bool = True,
    ) -> Any:
        candidates = self._candidates(category, cap)
        if not candidates:
            raise DataUnavailableError(f"没有任何数据源支持 {cap.value}", category=category)
        errors: list[str] = []
        skipped: list[str] = []
        for p in candidates:
            if p.health.is_open:
                skipped.append(f"{p.name}(熔断中)")
                continue
            if not p.is_available():
                skipped.append(f"{p.name}(依赖未就绪)")
                continue
            import time as _t

            t0 = _t.perf_counter()
            try:
                result = fn(p)
            except NotImplementedError as exc:
                skipped.append(f"{p.name}(未实现)")
                errors.append(f"{p.name}: {exc}")
                continue
            except SourceSkipped as exc:
                # 源主动声明「本次不服务」：跳过它走下一个源，**不记健康度失败、
                # 不熔断**（避免 QMT 财务超时误伤同样健康的 QMT 行情）。
                skipped.append(f"{p.name}(主动跳过)")
                errors.append(f"{p.name}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - 需要捕获所有源异常做降级
                p.health.record_failure(str(exc))
                errors.append(f"{p.name}: {exc}")
                logger.warning("数据源 %s 调用失败(%s): %s", p.name, context or cap.value, exc)
                continue
            empty = result is None or (hasattr(result, "empty") and result.empty) or (
                isinstance(result, (list, dict)) and len(result) == 0
            )
            if empty and not allow_empty:
                errors.append(f"{p.name}: 返回空")
                if empty_is_fault:
                    p.health.record_failure("空结果")
                continue
            p.health.record_success(_t.perf_counter() - t0)
            return result
        raise DataUnavailableError(
            f"全部数据源失败: {cap.value} {context}",
            errors="; ".join(errors) or "-",
            skipped="; ".join(skipped) or "-",
        )

    # 当日日线**绝对不可能存在**的时段：09:15 开盘集合竞价之前无任何当日行情，
    # 非交易日（周末/法定节假日）全天休市。只有这两种情况才把探测上界压回上一交易日。
    # ★ 盘中（AUCTION_OPEN 起）**不可**收敛：数据源会提供当日「正在形成」的日线 bar，
    #   把它挡掉会让行情页整日冻结在昨收、直到 24h 内存 TTL 过期（已实测踩坑）。
    _NO_TODAY_BAR_SESSIONS = frozenset({Session.CLOSED, Session.PRE_OPEN})

    def _clamp_probe_end(self, end, *, now: datetime | None = None) -> date | None:
        """把日线增量探测的上界收敛到「最近一个**可能已有数据**的交易日」。

        K线图页面与回测普遍传 ``end=今天``，而在盘前（<09:15）与非交易日，
        ``[cached_end+1, 今天]`` 这段区间**注定没有任何数据**，向数据源探测只会
        白等一轮降级链超时（实测 ~6s/次），失败结果还不被记忆 → 每点一只个股重付一次。

        收敛只在 :data:`_NO_TODAY_BAR_SESSIONS` 命中时生效。盘中与收盘后一律原样放行，
        因为当日「正在形成」的日线 bar 需要实时刷新。

        历史区间（回测 asof 落在过去）不受影响：``probe_cap`` 恒在其右侧，
        ``min`` 原样返回 ``end``，因此本方法对回测取数是完全的 no-op。

        :param now: 仅用于测试注入固定时钟；生产调用一律省略（取当前时间）。
        """
        try:
            end_d = pd.Timestamp(end).date()
        except (TypeError, ValueError):
            return None
        try:
            now = now or datetime.now()
            if self._cal.session_of(now) not in self._NO_TODAY_BAR_SESSIONS:
                return end_d                       # 盘中/收盘后：当日 bar 可能存在，不收敛
            # 盘前或非交易日 → 当日无 bar，上界压到上一个交易日（CLOSED 时 align 亦回溯到它）
            probe_cap = self._cal.prev_trading_day(now.date())
        except Exception as exc:                                # noqa: BLE001
            logger.debug("交易日历收敛探测上界失败，按原样探测: %s", exc)
            return end_d
        return min(end_d, probe_cap)

    def _today_refresh_due(
        self,
        end,
        cached_end,
        key: tuple,
        *,
        now: datetime | None = None,
    ) -> bool:
        """缓存末端**已覆盖**请求 end 时，判断是否仍需重探「今天」这根正在形成的 bar。

        与 :meth:`_clamp_probe_end` 互补：后者管「缓存落后于 end」的缺口补齐，
        本方法管「缓存追平 end、但 end 就是今天」的当日 bar 刷新。

        刷新只在四个条件同时成立时发生：

        1. 请求 ``end`` 恰为**今天** —— 历史 end（回测 asof 落在过去）直接 no-op，
           因此本方法对回测取数是完全的 no-op，PIT 语义不受任何影响；
        2. 缓存末端就是今天（否则属于缺口补齐，走另一条分支）；
        3. 当前时段不在 :data:`_NO_TODAY_BAR_SESSIONS`（盘前/非交易日今天不可能有 bar）；
        4. 距上次刷新已超过 ``today_bar_refresh_ttl`` —— 把探测频率钉死在每分钟一次
           以内，切股秒开的体验不能因为刷新而回退。

        纯判定，无副作用；调用方决定探测时须自行记时（见 get_bars）。

        :param key: 内存缓存 key，刷新计时按缓存条目独立（不同 start 各自计时）。
        :param now: 仅用于测试注入固定时钟；生产调用一律省略（取当前时间）。
        """
        if self.today_bar_refresh_ttl <= 0:
            return False
        try:
            now = now or datetime.now()
            today = now.date()
            if pd.Timestamp(end).date() != today:
                return False                            # 历史/未来 end：回测语义，绝不刷新
            if pd.Timestamp(cached_end).date() != today:
                return False                            # 缓存末端不是今天：交给缺口补齐
            if self._cal.session_of(now) in self._NO_TODAY_BAR_SESSIONS:
                return False                            # 今天不可能有 bar
            last = self._today_refresh_at.get(key)
            if last is not None and (time.time() - last) < self.today_bar_refresh_ttl:
                return False                            # 冷却窗内：沿用缓存
        except Exception as exc:                                # noqa: BLE001
            logger.debug("当日 bar 刷新判定失败，跳过刷新: %s", exc)
            return False
        return True

    # ------------------------------------------------------------ 行情
    def get_bars(
        self,
        symbols: Sequence[str] | str,
        freq: Freq = Freq.D1,
        start: date | str | None = None,
        end: date | str | None = None,
        adjust: Adjust = Adjust.HFQ,
        *,
        asof: date | datetime | None = None,
        validate: bool = True,
    ) -> pd.DataFrame:
        syms = _sym_list(symbols) or []
        if not syms:
            return pd.DataFrame()
        freq = freq if isinstance(freq, Freq) else Freq(freq)
        adjust = adjust if isinstance(adjust, Adjust) else Adjust(adjust)
        effective_asof = asof if asof is not None else self.asof
        category = "daily_bar" if freq == Freq.D1 else "minute_bar"
        # 范围感知缓存只对日线生效：分钟线盘中实时变化，逐次拉取。
        range_cache = category == "daily_bar"

        # ---- 范围感知缓存 key：**不含 end**（性能修复 2026-08-12）----
        # 历史日线不可变，缓存按 (syms, freq, start, adjust) 覆盖 [start, ∞)，
        # 请求的 end 只决定"切片到哪天"。回测 49 天每天 end 不同，
        # 旧 key（含 end）导致每天缓存不命中、全量重拉（~20s/天 ≈ 16 分钟）。
        # 分钟线盘中实时变化，不做范围感知，保留含 end 的旧 key。
        if range_cache:
            key = (tuple(syms), freq.value, str(start), adjust.value)
        else:
            key = (tuple(syms), freq.value, str(start), str(end), adjust.value)
        cached = self.cache.get(category, key)
        if cached is None:
            if range_cache:
                # 磁盘持久化缓存（仅日线）按**标的**分片落盘：命中的直接复用，
                # 只对未命中的标的向数据源下载（b1）。跨进程/跨池重跑不再全量重拉。
                hits, missing = self._load_bars_disk(syms, freq, start, adjust)
                frames = list(hits.values())
                if missing:
                    try:
                        downloaded = self._dispatch(
                            "bars",
                            Capability.BARS,
                            lambda p: p.get_bars(missing, freq, start, end, adjust),
                            context=f"bars {len(missing)} symbols",
                            # 单标的取数返回空通常是停牌/未收录（合法业务答案），不计熔断；
                            # 否则几只停牌票就能沿降级链把全部行情源熔断掉。
                            empty_is_fault=len(missing) > 1,
                        )
                        downloaded = self._normalize_bars(downloaded)
                        if downloaded is not None and not downloaded.empty:
                            frames.append(downloaded)
                    except DataUnavailableError:
                        # 无任何命中又下载失败 → 保持原 fail-safe 向上抛；
                        # 已有部分命中 → 沿用命中标的（新增标的可能是停牌，不拖垮全池）。
                        if not frames:
                            raise
                        logger.warning("部分标的(%d)行情下载失败，沿用已命中的 %d 个磁盘缓存标的",
                                       len(missing), len(frames))
                # 合并多标的后按 symbol 重算 prev_close，保证与单次全量拉取一致。
                cached = (self._normalize_bars(pd.concat(frames, ignore_index=True))
                          if frames else pd.DataFrame())
                if validate and not cached.empty:
                    warn_quality(self.validate_bars(cached),
                                 interval=self.quality_warn_interval)
                # 只落盘本次真正下载到的标的（命中的已在盘上，无需重写）。
                if missing and not cached.empty:
                    fresh = cached[cached["symbol"].isin(set(missing))]
                    if not fresh.empty:
                        self._save_bars_disk(fresh, freq, start, adjust)
            else:
                cached = self._dispatch(
                    "bars",
                    Capability.BARS,
                    lambda p: p.get_bars(syms, freq, start, end, adjust),
                    context=f"bars {len(syms)} symbols",
                    empty_is_fault=len(syms) > 1,
                )
                cached = self._normalize_bars(cached)
                if validate:
                    warn_quality(self.validate_bars(cached),
                                 interval=self.quality_warn_interval)
            self.cache.set(category, key, cached)
            # 首次下载已覆盖到「今天」→ 记一次刷新时刻，抑制紧接着增量分支对
            # [今天,今天] 的冗余重探（case B）。这份数据是毫秒前刚下载的，无需再探；
            # akshare 兜底时那次重探是又一轮 ~7s 全量 HTTP，直接拖累"首点秒开"。
            # 仅在 end 就是今天、且下载末端确为今天时才记：历史 end（回测）不记，
            # 既不污染 _today_refresh_at，_today_refresh_due 对历史 end 也本就不触发。
            if (range_cache and end is not None and not cached.empty
                    and pd.Timestamp(end).date() == datetime.now().date()
                    and pd.to_datetime(cached["date"]).max().date()
                    == pd.Timestamp(end).date()):
                self._today_refresh_at[key] = time.time()

        # ---- 增量探测：只拉需要的日期，绝不重拉全量 ----
        # 两种情形共用同一套探测/负缓存/合并逻辑，只是区间算法不同：
        #   A) cached_end < end   → 缺口补齐 [cached_end+1, 收敛后的 end]
        #   B) cached_end == end == 今天 → 当日 bar 仍在形成，按 TTL 重探 [今天, 今天]
        if range_cache and end is not None and cached is not None and not cached.empty:
            cached_end = pd.to_datetime(cached["date"]).max()
            if cached_end < pd.Timestamp(end):
                # 探测上界先收敛到「最近一个可能已有数据的交易日」。盘前(<09:15)与
                # 非交易日收敛后区间为空 → 整段探测直接跳过，一次数据源都不碰
                # （K线图盘前切股秒开的关键）。盘中/收盘后原样放行，保证当日 bar 实时刷新。
                probe_start = cached_end.date() + timedelta(days=1)
                probe_end = self._clamp_probe_end(end)
            elif self._today_refresh_due(end, cached_end, key):
                # 情形 B：先记时再探测，保证探测频率被 TTL 钉死（即使本次失败/无变化）。
                probe_start = probe_end = pd.Timestamp(end).date()
                self._today_refresh_at[key] = time.time()
            else:
                probe_start = probe_end = None
            if probe_end is not None and probe_start <= probe_end:
                # 探测负缓存：同一区间刚探测失败过，冷却窗内不再重试。
                # key 不含 symbol（失败根因是"数据源在该区间尚无数据"，与标的无关），
                # 但含 probe_start/probe_end，避免宽区间被窄区间的失败误伤。
                probe_key = (freq.value, adjust.value, str(probe_start), str(probe_end))
                fail_at = self._delta_probe_fail_at.get(probe_key)
                if fail_at is not None and self.delta_probe_cooldown > 0 \
                        and (time.time() - fail_at) < self.delta_probe_cooldown:
                    logger.debug("跳过行情增量探测（%s~%s）：%.0fs 内已失败过，沿用缓存",
                                 probe_start, probe_end, self.delta_probe_cooldown)
                else:
                    tail_before = _tail_fingerprint(cached, probe_start)
                    try:
                        delta = self._dispatch(
                            "bars",
                            Capability.BARS,
                            lambda p: p.get_bars(syms, freq, probe_start, probe_end, adjust),
                            context=f"bars增量 {len(syms)}",
                            empty_is_fault=False,
                        )
                        delta = self._normalize_bars(delta)
                        if delta is not None and not delta.empty:
                            merged = pd.concat([cached, delta], ignore_index=True)
                            # keep="last"：情形 B 下 delta 与 cached 在「今天」这行重叠，
                            # 必须让**新值覆盖旧值**（pandas 默认 keep="first" 会保住那根
                            # 冻结的 bar，把整次刷新白做一遍）。情形 A 无重叠，两种 keep 等价。
                            merged = (merged
                                      .drop_duplicates(["symbol", "date"], keep="last")
                                      .sort_values(["symbol", "date"])
                                      .reset_index(drop=True))
                            # 增量合并后 prev_close 需基于全表重算（增量首行依赖缓存末行）
                            merged = self._normalize_bars(merged)
                            if _tail_fingerprint(merged, probe_start) != tail_before:
                                cached = merged
                                self.cache.set(category, key, cached)
                                self._save_bars_disk(cached, freq, start, adjust)
                            # 数据无变化（夜间/停牌/当日 bar 未动）→ 不重写内存与磁盘，
                            # 避免每分钟一次无谓的 parquet + DuckDB 写入与实盘争锁。
                        # 探测成功 → 清掉可能残留的失败标记
                        self._delta_probe_fail_at.pop(probe_key, None)
                    except DataUnavailableError:
                        # 记入负缓存。此分支同时覆盖"全源抛异常"与"全源返回空"
                        # （_dispatch 在 allow_empty=False 时把空返回也当失败耗尽候选链）。
                        self._delta_probe_fail_at[probe_key] = time.time()
                        logger.warning(
                            "行情增量取数失败（%s~%s），沿用已有缓存；%.0fs 内不再重试该区间",
                            probe_start, probe_end, self.delta_probe_cooldown)

        df = cached
        # 范围感知：缓存可能覆盖到 end 之后，按请求 end 切片。
        # 分钟线的 date 带时分秒，end=YYYY-MM-DD 语义是「含当天全天」，
        # 直接 <= pd.Timestamp(end)（当天 00:00:00）会把当日分钟 bar 全部滤掉
        # （表现为分时图永远停在上一交易日），必须按日期比较。
        if end is not None:
            dates = pd.to_datetime(df["date"])
            if range_cache:
                df = df[dates <= pd.Timestamp(end)]
            else:
                df = df[dates.dt.date <= pd.Timestamp(end).date()]
        df = self._slice_frame(df, effective_asof, "date", "bars")
        return df.reset_index(drop=True)

    # ---- 日线磁盘持久化缓存（范围感知缓存的跨运行层）----
    # 历史日线不可变 → "拉一次落盘，重跑直接读"。HFQ 复权因子会随新除权整体漂移，
    # 故用写入时间窗（12 小时）校验：过期即丢弃重拉，避免陈旧复权序列。
    # 元数据用 sidecar json（DataFrame.attrs 不保证被 parquet 引擎持久化）。
    _BARS_DISK_TTL = 12 * 3600
    # 落盘 schema 版本号：列语义/单位变更时 +1，使旧缓存自动失效。
    # v2（2026-08-13）：QMT volume 由手归一为股，旧落盘 volume 小 100 倍必须弃用。
    # v3（2026-08-13）：曾出现 QMT 本地缺最近日线时缓存到「日期错位」序列
    # （尾行停在 T-1，策略用错日数据判涨幅/新高），全量作废重拉。
    _BARS_DISK_SCHEMA = 3
    # 过期缓存清理节流间隔（秒）：写路径触发，每进程每小时至多一次（b2）。
    _BARS_EVICT_INTERVAL = 3600

    def _bars_disk_enabled(self) -> bool:
        """磁盘持久化仅对**真实数据源**启用：mock 是合成数据（不同实例参数生成的
        序列不同），落盘只会造成跨进程串库（曾导致 smoke_selection 偶发失败）。"""
        return self.bars_cache_dir is not None and "mock" not in self.providers

    def _bars_disk_sym_key(self, symbol: str, freq: Freq, start, adjust: Adjust) -> tuple:
        """按标的的磁盘 key：单标的 + freq + start + adjust + 数据源集合 + schema。

        旧 _bars_disk_key 把整个股票池塞进 key（全市场硬筛一次 5,219 只 → 67,912
        字符）：池组成每变一次 key 就完全不同、永不命中，每次扫描/回测都从数据源
        全量重拉全市场日线——系统卡顿根因（b1）。改为按标的分片后，重叠标的跨池
        复用，只下载真正新增的标的，且每个 key 定长极短。
        """
        return (symbol, freq.value, str(start), adjust.value,
                tuple(sorted(self.providers)), self._BARS_DISK_SCHEMA)

    def _load_bars_disk(self, syms, freq, start, adjust):
        """按标的批量加载磁盘缓存，返回 (命中的 {symbol: DataFrame}, 未命中的 [symbol])。"""
        if not self._bars_disk_enabled():
            return {}, list(syms)
        key_by_sym = {s: repr(self._bars_disk_sym_key(s, freq, start, adjust)) for s in syms}
        try:
            frames = self.store.read_many_batch(
                "bars_cache", list(key_by_sym.values()), ttl_seconds=self._BARS_DISK_TTL)
        except Exception as exc:  # noqa: BLE001 - 缓存读失败降级为重拉
            logger.warning("日线磁盘缓存批量读取失败，转为重拉: %s", exc)
            return {}, list(syms)
        hits: dict[str, pd.DataFrame] = {}
        missing: list[str] = []
        for sym, cache_key in key_by_sym.items():
            df = frames.get(cache_key)
            if df is not None and not df.empty:
                hits[sym] = df
            else:
                missing.append(sym)
        return hits, missing

    def _save_bars_disk(self, df: pd.DataFrame, freq, start, adjust) -> None:
        """按标的分片落盘：df 内每个 symbol 各自一个 key（池变化时重叠标的仍复用）。"""
        if not self._bars_disk_enabled() or df is None or df.empty:
            return
        try:
            self.store.write_partitioned(
                "bars_cache", df,
                key_fn=lambda s: repr(self._bars_disk_sym_key(s, freq, start, adjust)),
                partition_col="symbol")
        except Exception as exc:  # noqa: BLE001 - 缓存写失败不影响主流程
            logger.warning("日线磁盘缓存分片写入失败: %s", exc)
        self._evict_bars_disk()

    def _evict_bars_disk(self) -> None:
        """淘汰过期的日线磁盘缓存（b2）。

        TTL 此前只影响读命中，过期行永不删除，随股票池变化无限堆积（磁盘只增
        不减）。写路径触发、每小时至多一次，把磁盘占用锁死在「活跃 key」范围内。
        淘汰失败不影响主流程（只是少清一次，下次写入再试）。
        """
        import time
        if not self._bars_disk_enabled():
            return
        now = time.time()
        if now - self._last_bars_evict < self._BARS_EVICT_INTERVAL:
            return
        self._last_bars_evict = now
        try:
            removed = self.store.evict_expired("bars_cache", self._BARS_DISK_TTL, now=now)
            if removed:
                logger.info("清理过期日线缓存 %d 个 key", removed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("过期日线缓存清理失败: %s", exc)

    # ------------------------------------------------------------ PIT 切片helper
    def _slice_frame(
        self,
        df: pd.DataFrame,
        asof: date | datetime | None,
        time_col: str,
        label: str,
    ) -> pd.DataFrame:
        """按 asof 裁剪 DataFrame。

        裁剪本身是 DataHub 的正常职责，用 strict=False 静默执行；
        裁剪后若 ``strict_pit`` 打开，再自检一次，确保切片逻辑本身没有 bug。
        """
        if asof is None or df is None or df.empty:
            return df if df is not None else pd.DataFrame()
        out = PITGuard(asof, strict=False).filter_frame(df, time_col=time_col, label=label)
        if self.strict_pit:
            PITGuard(asof, strict=True).filter_frame(out, time_col=time_col, label=f"{label}:自检")
        return out

    def _slice_records(
        self,
        records: Sequence[Any],
        asof: date | datetime | None,
        time_attr: str,
        label: str,
    ) -> list:
        if asof is None or not records:
            return list(records or ())
        out = PITGuard(asof, strict=False).filter_records(records, time_attr, label=label)
        if self.strict_pit:
            PITGuard(asof, strict=True).filter_records(out, time_attr, label=f"{label}:自检")
        return out

    def _normalize_bars(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        # 不复权(NONE)/首行等情况下，源可能返回 NaN 的 prev_close。
        # 不论列是否存在都始终用 close 回填，避免后续 limit 计算抛「非法价格」。
        if "prev_close" not in df.columns:
            df["prev_close"] = df.groupby("symbol")["close"].shift(1)
        df["prev_close"] = df["prev_close"].fillna(df["close"])
        if "is_suspended" not in df.columns:
            df["is_suspended"] = df.get("volume", 1) <= 0
        if "amount" not in df.columns:
            df["amount"] = df["close"] * df.get("volume", 0)
        if "limit_up" not in df.columns:
            profiles = {s: build_profile(s) for s in df["symbol"].unique()}

            def _lim(fn, pc):
                return fn(pc) if (pc is not None and not pd.isna(pc)) else float("nan")

            df["limit_up"] = [
                _lim(profiles[s].limit_up, pc) for s, pc in zip(df["symbol"], df["prev_close"])
            ]
            df["limit_down"] = [
                _lim(profiles[s].limit_down, pc) for s, pc in zip(df["symbol"], df["prev_close"])
            ]
        return df.sort_values(["symbol", "date"]).reset_index(drop=True)

    def _suspension_mask(self, df: pd.DataFrame) -> pd.Series:
        """停牌行掩码。``_normalize_bars`` 会补 ``is_suspended``，但外部直接调
        ``validate_bars`` 的帧（回测/单测手工拼的）可能没这一列，这里就地兜底。"""
        if "is_suspended" in df.columns:
            return df["is_suspended"].fillna(False).astype(bool)
        if "volume" in df.columns:
            return df["volume"].fillna(0) <= 0
        return pd.Series(False, index=df.index)

    def _new_listing_mask(self, df: pd.DataFrame) -> pd.Series:
        """处于新股期（上市 ≤ ``new_listing_grace_days`` 天）的行掩码。

        **只读 ``_instrument_cache``，绝不在此派单取数**：``get_instrument`` 未命中时
        会去数据源拉一次标的详情，那等于把「体检函数」变成新的故障源与延迟源，
        还会在回测里引入 asof 之外的信息。缓存没有就当不是新股——宁可多报一条
        issue，也不能为了少报而偷偷发网络请求。
        """
        mask = pd.Series(False, index=df.index)
        if (self.new_listing_grace_days <= 0 or not self._instrument_cache
                or "symbol" not in df.columns or "date" not in df.columns):
            return mask
        dates = pd.to_datetime(df["date"])
        grace = pd.Timedelta(days=self.new_listing_grace_days)
        for sym, info in self._instrument_cache.items():
            if not getattr(info, "list_date", None):
                continue
            hit = df["symbol"] == sym
            if not bool(hit.any()):
                continue
            mask |= hit & (dates <= pd.Timestamp(info.list_date) + grace)
        return mask

    def validate_bars(self, df: pd.DataFrame) -> QualityReport:
        """数据质量校验（设计 6.1.3）。

        结论分两级，详见 :class:`QualityReport`。两类 A 股**合法**极端值被显式豁免、
        只记 note：

        1. **停牌日**——数据源把 OHLC 记为 0/NaN、volume 记为 0。旧实现因此报
           「low 存在非正价格」，而 ``data_sync`` 属 CRITICAL 任务，一条这样的
           issue 就足以把全系统降级到 REDUCE_ONLY。
        2. **停牌复牌 / 新股期的大幅跳变**——复牌日相对停牌前一日的补跌补涨、
           新股上市初期的无限制波动，都能轻松超过 ``max_abs_return``。

        剩下的真异常会带上**条数 + 标的@日期样本**，否则一行「low 存在非正价格」
        既看不出规模也定位不到标的，只能当噪音忽略。
        """
        rep = QualityReport()
        if df is None or df.empty:
            rep.add("空数据集")
            return rep

        susp = self._suspension_mask(df)
        active = ~susp
        n_susp = int(susp.sum())
        if n_susp:
            rep.note(f"{n_susp} 条停牌记录（OHLC 可能为 0/NaN，数据源合法表达）已豁免校验")
        newl = self._new_listing_mask(df)
        n_newl = int(newl.sum())
        if n_newl:
            rep.note(f"{n_newl} 条新股期记录（上市 ≤{self.new_listing_grace_days} 天，"
                     f"无常规涨跌幅限制）已豁免涨跌幅校验")

        for col in ("open", "high", "low", "close"):
            if col not in df.columns:
                rep.add(f"缺列 {col}")
                continue
            bad = df[col].isna().mean()
            if bad > self.max_missing_ratio:
                rep.add(f"{col} 缺失率 {bad:.1%} 超阈值")
            # 非正价格只在**有成交**的行上才算异常
            nonpos = (df[col].fillna(1) <= 0) & active
            n_np = int(nonpos.sum())
            if n_np:
                rep.add(f"{col} 存在非正价格 {n_np} 条（{_bar_samples(df, nonpos)}）")

        if {"high", "low"} <= set(df.columns):
            inv = (df["high"] < df["low"]) & active
            n_inv = int(inv.sum())
            if n_inv:
                rep.add(f"存在 high < low 的记录 {n_inv} 条（{_bar_samples(df, inv)}）")

        if {"close", "prev_close"} <= set(df.columns):
            ret = (df["close"] / df["prev_close"].replace(0, pd.NA) - 1).abs()
            # prev_close 为 0/NaN 时比值是 NA，比较结果也是 NA；用 boolean 中转再
            # 落到 bool，避免 object dtype 下 fillna 的行为差异。
            over = ((ret > self.max_abs_return).astype("boolean")
                    .fillna(False).astype(bool) & active)
            # 复牌日：本行停牌，或**同标的**上一行停牌（停牌前一日的收盘价与复牌价
            # 之间可以隔着十几个交易日的基本面变化）。按 symbol 分组 shift，避免跨
            # 标的把上一只票的停牌状态串到下一只票的首行上。
            if "symbol" in df.columns:
                prev_susp = susp.groupby(df["symbol"]).shift(1, fill_value=False)
            else:
                prev_susp = susp.shift(1, fill_value=False)
            explainable = over & (susp | prev_susp.astype("boolean").fillna(False).astype(bool)
                                  | newl)
            real = over & ~explainable
            n_ex, n_real = int(explainable.sum()), int(real.sum())
            if n_ex:
                rep.note(f"{n_ex} 条涨跌幅超 {self.max_abs_return:.0%} 可归因于"
                         f"停牌复牌/新股期（{_bar_samples(df, explainable)}）")
            if n_real:
                rep.add(f"{n_real} 条记录涨跌幅超过 {self.max_abs_return:.0%}"
                        f"（{_bar_samples(df, real)}）")
        return rep

    def require_clean_bars(self, df: pd.DataFrame) -> pd.DataFrame:
        rep = self.validate_bars(df)
        if not rep.ok:
            raise DataQualityError(f"行情数据未通过质量校验: {rep}")
        return df

    def get_index_bars(
        self,
        index_symbol: str,
        start: date | str | None = None,
        end: date | str | None = None,
        *,
        asof: date | datetime | None = None,
    ) -> pd.DataFrame:
        key = (index_symbol, str(start), str(end))
        df = self.cache.get("index", key)
        if df is None:
            df = self._dispatch(
                "bars",
                Capability.INDEX,
                lambda p: p.get_index_bars(index_symbol, start, end),
                context=f"index {index_symbol}",
            )
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            self.cache.set("index", key, df)
        effective = asof if asof is not None else self.asof
        df = self._slice_frame(df, effective, "date", "index")
        return df.reset_index(drop=True)

    def get_realtime(self, symbols: Sequence[str]) -> dict[str, Tick]:
        """实时行情。**永不缓存**（修正 TradingAgents-CN 的 1 小时 TTL 缺陷）。"""
        syms = _sym_list(symbols) or []
        return self._dispatch(
            "bars", Capability.TICK, lambda p: p.get_realtime(syms), context="realtime"
        )

    # ------------------------------------------------------------ 基础信息
    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[InstrumentInfo]:
        syms = _sym_list(symbols)
        # 全量标的池请求（空参）在实盘模式(asof is None)下走 TTL 缓存：/market/symbols
        # 每次点 tab 都会空参拉全市场（akshare 全量快照 / QMT 逐只 detail，1~5s+），
        # 而标的名单日内极稳定，缓存后重复请求近乎瞬时。
        # 回测/复现(asof 有值)绝不缓存：标的名单随 asof 变化，复用会引入前视偏差。
        use_universe_cache = not syms and self.asof is None and self.universe_ttl > 0
        if use_universe_cache:
            if (self._universe_cache is not None
                    and (time.time() - self._universe_cache_at) < self.universe_ttl):
                return self._universe_cache
        elif syms and all(s in self._instrument_cache for s in syms):
            return [self._instrument_cache[s] for s in syms]
        infos = self._dispatch(
            "instruments",
            Capability.INSTRUMENTS,
            lambda p: p.get_instruments(syms),
            context="instruments",
            # 「源里没有这只票」是合法业务答案（未收录/未覆盖），不是源故障，
            # 不计熔断——否则持仓页查几个源外名称就能把唯一数据源熔断掉。
            empty_is_fault=False,
        )
        for info in infos:
            self._instrument_cache[info.symbol] = info
        # 只缓存非空的全量结果：源临时故障返回空时不写，避免把空名单锁定一个 TTL 周期。
        if use_universe_cache and infos:
            self._universe_cache = infos
            self._universe_cache_at = time.time()
        return infos

    def get_instrument(self, symbol: str) -> InstrumentInfo | None:
        sym = normalize_symbol(symbol)
        if sym in self._instrument_missing:
            return None                       # 负缓存：源外标的不反复派单
        if sym not in self._instrument_cache:
            try:
                self.get_instruments([sym])
            except DataUnavailableError:
                self._instrument_missing.add(sym)
                return None
        return self._instrument_cache.get(sym)

    # ------------------------------------------------------------ 财务
    def get_fundamentals(
        self,
        symbols: Sequence[str],
        start: date | str | None = None,
        end: date | str | None = None,
        *,
        asof: date | datetime | None = None,
    ) -> list[Fundamental]:
        syms = _sym_list(symbols) or []
        key = (tuple(syms), str(start), str(end))
        data = self.cache.get("fundamental", key)
        if data is None:
            data = self._dispatch(
                "fundamentals",
                Capability.FUNDAMENTALS,
                lambda p: p.get_fundamentals(syms, start, end),
                context="fundamentals",
                allow_empty=True,
            )
            self.cache.set("fundamental", key, data)
        effective = asof if asof is not None else self.asof
        # F3：财务按 ann_date（公告日）切片 —— PIT 判定依据，不依赖 publish_time property
        return self._slice_records(data, effective, "ann_date", "fundamentals")

    def get_latest_fundamentals(
        self, symbols: Sequence[str], *, asof: date | datetime | None = None
    ) -> dict[str, Fundamental]:
        """每个标的在 asof 时点**已公告**的最新一期。财务因子必须用这个。"""
        from .pit import latest_fundamental_asof

        effective = asof if asof is not None else self.asof
        records = self.get_fundamentals(symbols, asof=None)
        if effective is None:
            effective = datetime.now()
        return latest_fundamental_asof(records, effective)

    # ------------------------------------------------------------ 新闻与事件
    def get_news(
        self,
        symbols: Sequence[str] | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 200,
        *,
        asof: date | datetime | None = None,
    ) -> list[NewsItem]:
        syms = _sym_list(symbols)
        # 性能修复（2026-08-12）：news 走 DataHub 内存缓存。
        # 此前每次调用都重新联网逐票拉全量（回测 4500+ 只 × 49 天 ≈ 98 小时）；
        # 新闻是**不可变历史**，同一 (syms,start,end,limit) 请求跨日应直接命中缓存。
        key = (tuple(syms), str(start), str(end), int(limit))
        items = self.cache.get("news", key)
        if items is None:
            try:
                items = self._dispatch(
                    "news",
                    Capability.NEWS,
                    lambda p: p.get_news(syms, start, end, limit),
                    context="news",
                    allow_empty=True,
                )
            except DataUnavailableError:
                logger.warning("新闻源全部不可用，返回空列表（不阻断主流程）")
                return []
            self.cache.set("news", key, items)
        # 真实源（akshare）不带情绪字段：出口统一用规则词典补分（幂等，重复调用安全）
        for n in items:
            if n.sentiment is None:
                n.sentiment = score_sentiment(n.title, n.content)
        effective = asof if asof is not None else self.asof
        return self._slice_records(items, effective, "publish_time", "news")

    def get_events(
        self,
        symbols: Sequence[str] | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        *,
        asof: date | datetime | None = None,
    ) -> list[CorpEvent]:
        syms = _sym_list(symbols)
        # 性能修复（2026-08-12）：events 走 DataHub 内存缓存（底层即新闻，不可变历史）。
        key = (tuple(syms), str(start), str(end))
        items = self.cache.get("events", key)
        if items is None:
            try:
                items = self._dispatch(
                    "news",
                    Capability.EVENTS,
                    lambda p: p.get_events(syms, start, end),
                    context="events",
                    allow_empty=True,
                )
            except DataUnavailableError:
                return []
            self.cache.set("events", key, items)
        effective = asof if asof is not None else self.asof
        return self._slice_records(items, effective, "publish_time", "events")

    def get_money_flow(
        self,
        symbols: Sequence[str],
        start: date | str | None = None,
        end: date | str | None = None,
        *,
        asof: date | datetime | None = None,
    ) -> pd.DataFrame:
        syms = _sym_list(symbols) or []
        try:
            df = self._dispatch(
                "bars",
                Capability.MONEY_FLOW,
                lambda p: p.get_money_flow(syms, start, end),
                context="money_flow",
                allow_empty=True,
            )
        except DataUnavailableError:
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        effective = asof if asof is not None else self.asof
        df = self._slice_frame(df, effective, "date", "money_flow")
        return df.reset_index(drop=True)

    # ------------------------------------------------------------ 运维
    def health_snapshot(self) -> list[dict]:
        return [p.health.snapshot() for p in self.providers.values()]

    def is_healthy(self) -> bool:
        """至少有一个行情源可用，否则系统应进入失败安全。"""
        return any(
            (not p.health.is_open) and p.is_available() and p.supports(Capability.BARS)
            for p in self.providers.values()
        )
