"""DataHub —— 统一数据接口 + 优先级降级 + PIT + 质量校验 + 缓存。

对应设计 6.1。三个关键点：

1. **所有接口都有 ``asof``**，PIT 在这一层统一施加，下游模块不需要自己操心穿越问题；
2. **降级不是无脑重试**：熔断打开的源直接跳过，不浪费一次超时；
3. **全部源失败抛 DataUnavailableError**，由调度层转为「当日停止开仓」（P4）。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd

from ..core.config import Settings
from ..core.errors import DataQualityError, DataUnavailableError
from ..core.instruments import build_profile, normalize_symbol
from ..core.logging import get_logger
from .cache import CategoryCache
from .pit import PITGuard
from .providers.base import Capability, DataProvider
from .sentiment import score_sentiment
from .store import ParquetStore
from .types import Adjust, CorpEvent, Freq, Fundamental, InstrumentInfo, NewsItem, Tick, SourceSkipped

logger = get_logger("datahub.manager")


def _sym_list(symbols) -> list[str] | None:
    """统一入参：允许传单个字符串、序列或 None。"""
    if symbols is None:
        return None
    if isinstance(symbols, str):
        symbols = [symbols]
    return [normalize_symbol(s) for s in symbols]


class QualityReport:
    def __init__(self):
        self.issues: list[str] = []

    def add(self, msg: str) -> None:
        self.issues.append(msg)

    @property
    def ok(self) -> bool:
        return not self.issues

    def __str__(self) -> str:  # pragma: no cover
        return "; ".join(self.issues) if self.issues else "OK"


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
        store: ParquetStore | None = None,
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
        self.store = store or ParquetStore(settings.data_dir / "parquet")
        # 行情磁盘持久化缓存目录（性能修复 2026-08-12）：
        # 历史日线"拉一次落盘，重跑直接读"，避免每次回测都从数据源重拉全量。
        self.bars_cache_dir = settings.data_dir / "bars_cache"
        try:
            self.bars_cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.bars_cache_dir = None
        self.asof = asof
        self.strict_pit = strict_pit
        quality = settings.section("datahub.quality")
        self.max_missing_ratio = float(quality.get("max_missing_ratio", 0.2))
        self.max_abs_return = float(quality.get("max_abs_return", 0.35))
        self._instrument_cache: dict[str, InstrumentInfo] = {}
        self._instrument_missing: set[str] = set()

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
            # 磁盘持久化缓存（仅日线）：data/bars_cache/ 下按 key 指纹落盘，
            # 跨进程/跨运行重跑直接读，不再向数据源重拉全量。
            disk = self._load_bars_disk(key) if range_cache else None
            if disk is not None:
                cached = disk
            else:
                cached = self._dispatch(
                    "bars",
                    Capability.BARS,
                    lambda p: p.get_bars(syms, freq, start, end, adjust),
                    context=f"bars {len(syms)} symbols",
                    # 单标的取数返回空通常是停牌/未收录（合法业务答案），不计熔断；
                    # 否则几只停牌票就能沿降级链把全部行情源熔断掉。
                    empty_is_fault=len(syms) > 1,
                )
                cached = self._normalize_bars(cached)
                if validate:
                    report = self.validate_bars(cached)
                    if not report.ok:
                        logger.warning("行情数据质量问题: %s", report)
                if range_cache:
                    self._save_bars_disk(key, cached)
            self.cache.set(category, key, cached)

        # ---- 增量：请求 end 超出缓存覆盖范围时，只拉缺口合并（不重拉全量）----
        if range_cache and end is not None and cached is not None and not cached.empty:
            cached_end = pd.to_datetime(cached["date"]).max()
            if cached_end < pd.Timestamp(end):
                delta_start = cached_end.date() + timedelta(days=1)
                try:
                    delta = self._dispatch(
                        "bars",
                        Capability.BARS,
                        lambda p: p.get_bars(syms, freq, delta_start, end, adjust),
                        context=f"bars增量 {len(syms)}",
                        empty_is_fault=False,
                    )
                    delta = self._normalize_bars(delta)
                    if delta is not None and not delta.empty:
                        cached = pd.concat([cached, delta], ignore_index=True)
                        cached = (cached
                                  .drop_duplicates(["symbol", "date"])
                                  .sort_values(["symbol", "date"])
                                  .reset_index(drop=True))
                        # 增量合并后 prev_close 需基于全表重算（增量首行依赖缓存末行）
                        cached = self._normalize_bars(cached)
                        self.cache.set(category, key, cached)
                        self._save_bars_disk(key, cached)
                except DataUnavailableError:
                    logger.warning("行情增量取数失败（%s~%s），沿用已有缓存", delta_start, end)

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

    def _bars_disk_enabled(self) -> bool:
        """磁盘持久化仅对**真实数据源**启用：mock 是合成数据（不同实例参数生成的
        序列不同），落盘只会造成跨进程串库（曾导致 smoke_selection 偶发失败）。"""
        return self.bars_cache_dir is not None and "mock" not in self.providers

    def _bars_disk_key(self, key: tuple) -> tuple:
        """磁盘 key = 内存 key + 数据源集合（防不同源配置串库）+ schema 版本。"""
        return key + (tuple(sorted(self.providers)), self._BARS_DISK_SCHEMA)

    def _bars_disk_paths(self, key: tuple) -> tuple[Path | None, Path | None]:
        if self.bars_cache_dir is None:
            return None, None
        try:
            fp = hashlib.md5(repr(self._bars_disk_key(key)).encode("utf-8")).hexdigest()[:16]
            return (self.bars_cache_dir / f"bars_{fp}.parquet",
                    self.bars_cache_dir / f"bars_{fp}.meta.json")
        except Exception:  # noqa: BLE001
            return None, None

    def _load_bars_disk(self, key: tuple) -> pd.DataFrame | None:
        import json as _json
        import time as _t

        if not self._bars_disk_enabled():
            return None
        path, meta_path = self._bars_disk_paths(key)
        if path is None or not path.exists():
            return None
        try:
            meta: dict = {}
            if meta_path and meta_path.exists():
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("cache_key") != repr(self._bars_disk_key(key)):
                return None  # hash 冲突/串库：直接不用
            age = _t.time() - float(meta.get("written_at", path.stat().st_mtime))
            if age > self._BARS_DISK_TTL:
                path.unlink(missing_ok=True)
                return None
            df = pd.read_parquet(path)
            return df if not df.empty else None
        except Exception:  # noqa: BLE001
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def _save_bars_disk(self, key: tuple, df: pd.DataFrame) -> None:
        import json as _json
        import time as _t

        if not self._bars_disk_enabled() or df is None or df.empty:
            return
        path, meta_path = self._bars_disk_paths(key)
        if path is None:
            return
        try:
            df.to_parquet(path, index=False)
            _json.dump({"cache_key": repr(self._bars_disk_key(key)), "written_at": _t.time()},
                       open(meta_path, "w", encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("bars 磁盘缓存写入失败: %s", exc)

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

    def validate_bars(self, df: pd.DataFrame) -> QualityReport:
        """数据质量校验（设计 6.1.3）。"""
        rep = QualityReport()
        if df is None or df.empty:
            rep.add("空数据集")
            return rep
        for col in ("open", "high", "low", "close"):
            if col not in df.columns:
                rep.add(f"缺列 {col}")
                continue
            bad = df[col].isna().mean()
            if bad > self.max_missing_ratio:
                rep.add(f"{col} 缺失率 {bad:.1%} 超阈值")
            if (df[col].fillna(1) <= 0).any():
                rep.add(f"{col} 存在非正价格")
        if {"high", "low"} <= set(df.columns) and (df["high"] < df["low"]).any():
            rep.add("存在 high < low 的记录")
        if {"close", "prev_close"} <= set(df.columns):
            ret = (df["close"] / df["prev_close"].replace(0, pd.NA) - 1).abs()
            n_bad = int((ret > self.max_abs_return).sum())
            if n_bad:
                rep.add(f"{n_bad} 条记录涨跌幅超过 {self.max_abs_return:.0%}")
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
        if syms and all(s in self._instrument_cache for s in syms):
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
