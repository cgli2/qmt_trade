"""QMT / xtdata 行情源。盘中唯一可靠的实时数据来源。

xtquant 只在装了 miniQMT 的 Windows 机器上可用，因此全部为惰性导入，
在开发机与 CI 上 :meth:`is_available` 返回 False，自动降级到其它源。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

import pandas as pd

from ...core.instruments import normalize_symbol
from ...core.logging import get_logger
from ..types import (Adjust, Freq, Fundamental, InstrumentInfo, SourceSkipped, Tick)
from .base import Capability, DataProvider

logger = get_logger("datahub.qmt")

#: 项目根目录（qmt_trade/datahub/providers/ → 上溯 3 级），用于定位默认磁盘缓存目录。
_PROJ_ROOT = Path(__file__).resolve().parents[3]


def _num(v) -> float:
    """财务数值安全转换：None/NaN/字符串脏值 → NaN，不抛异常。"""
    try:
        f = float(v)
        return f if f == f else float("nan")  # NaN 保持 NaN
    except (TypeError, ValueError):
        return float("nan")

_FREQ_MAP = {
    Freq.D1: "1d",
    Freq.M1: "1m",
    Freq.M5: "5m",
    Freq.M15: "15m",
    Freq.M30: "30m",
    Freq.M60: "1h",
}
_ADJUST_MAP = {Adjust.NONE: "none", Adjust.QFQ: "front", Adjust.HFQ: "back"}

#: 盘中分钟数据增量下载的最小间隔（秒）：xtdata 只读本地数据且不会自动推送更新，
#: 若仅在「完全无数据」时才补下载，盘中会一直读到陈旧数据（分时图后半段缺口/失真）。
_INTRADAY_DL_INTERVAL = 30.0


def _parse_timetag(timetag) -> float:
    """QMT timetag 兼容解析：不同版本返回形态不一致——
    新版为数字毫秒时间戳（如 1786414964000），部分版本返回
    '20260811 10:24:44' 格式字符串。统一转成秒级 unix 时间戳，
    解析失败返回 0（调用方按 now() 兜底，不再整条 tick 丢弃）。
    """
    try:
        v = float(timetag)
        return v / 1000 if v > 0 else 0.0
    except (TypeError, ValueError):
        pass
    try:
        return datetime.strptime(str(timetag).strip(), "%Y%m%d %H:%M:%S").timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


class QmtProvider(DataProvider):
    name = "qmt"
    capabilities = {Capability.BARS, Capability.TICK, Capability.INSTRUMENTS,
                    Capability.INDEX, Capability.FUNDAMENTALS, Capability.MONEY_FLOW}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._xtdata = None
        self._subscribed: set[tuple[str, str]] = set()  # (symbol, period)
        self._last_dl: dict[tuple[str, str], float] = {}
        # 进程内按标的缓存财务解析结果：跨调仓日重复取数秒回；单次超时只丢
        # 缺失项，已缓存的不受影响（避免"首次超时→永久降级 akshare"退化）。
        self._fin_cache: dict[str, Fundamental] = {}
        # 财务跨进程磁盘缓存：目录默认 <项目根>/data/fundamentals，TTL 默认 1 天
        # （对齐 datahub.cache.fundamental_ttl），由 _make_provider 透传覆盖。
        self._fin_cache_dir = kwargs.get("cache_dir")
        self._fin_ttl = float(kwargs.get("fundamental_ttl", 86400) or 86400)
        # QMT 同步下载（download_history_data2 内部存在「is_connected() 为真但
        # 数据迟迟不到」的无限阻塞等待）——必须设硬性超时，超时即放弃并降级。
        # 默认 180s（足够正常下载，远小于"卡一整晚"）；可用环境变量覆盖。
        self._dl_timeout = float(
            os.environ.get("QMT_DL_TIMEOUT")
            or kwargs.get("download_timeout_sec", 180.0))

    def is_available(self) -> bool:
        # 行情/财务/资金流数据只需**本地 miniQMT 终端在跑**（xtdata 自动连
        # 127.0.0.1:58610，用户日志"xtdata连接成功"即证明可用）。
        # 之前强制要求 QMT_MINI_PATH（那是实盘下单网关的券商路径，见
        # execution/gateway/qmt.py）→ 未设环境变量时行情源被误判不可用，
        # 一路降级到 akshare（用户环境基本面/资金流取不到的直接原因之一）。
        # 终端未运行时调用会抛异常，由 DataHub 降级链捕获并转下一个源。
        try:
            from xtquant import xtdata  # noqa: F401
        except Exception:
            return False
        return True

    @property
    def xtdata(self):
        if self._xtdata is None:
            from xtquant import xtdata

            self._xtdata = xtdata
        return self._xtdata

    # ------------------------------------------------------------ 订阅
    def subscribe(self, symbols: Sequence[str], period: str = "tick") -> None:
        """订阅实时行情。重连后需要重新调用（QMTGateway 会负责）。

        period 默认 tick；分时图需 1m，故 get_bars(M1) 也会订阅 1m ——
        把**当日**分钟数据推入本地缓存，供 get_market_data_ex 读取
        （仅靠 download_history_data 回补当天未走完的分钟数据经常不到位，
         是「分时图停在上一交易日」的根因）。
        """
        for sym in symbols:
            s = normalize_symbol(sym)
            key = (s, period)
            if key in self._subscribed:
                continue
            try:
                self.xtdata.subscribe_quote(s, period=period)
                self._subscribed.add(key)
            except Exception as exc:  # noqa: BLE001 - 订阅失败不应击穿取数链
                logger.warning("QMT 订阅 %s(%s) 失败: %s", s, period, exc)

    def unsubscribe_all(self) -> None:
        self._subscribed.clear()

    # ------------------------------------------------------------ 断线重连容错
    def _reconnect(self) -> None:
        """QMT 行情连接偶发被服务端重置（xtdata 报 10054「远程主机强迫关闭连接」，

        错误 ``func=requestFromCache``、``isNetError=true``）。

        根因通常是 miniQMT 终端掉线/未登录/本地数据缓存服务断开，属环境问题；

        但**偶发**重置可尝试重建本地数据连接恢复。不同 xtdata 版本重连 API 不同，

        这里 duck-typed 尝试 ``connect()``，不存在则 no-op（交由上方 fail-closed）。
        """
        try:
            connect = getattr(self.xtdata, "connect", None)
            if callable(connect):
                connect()
                logger.info("QMT 行情连接已重新建立（connect()）")
        except Exception as exc:  # noqa: BLE001
            logger.debug("QMT 重连尝试失败（仍按原路径失败）: %s", exc)

    def _safe_market_data(self, syms, period, s, e, adjust):
        """包装 ``get_market_data_ex``：遇连接重置（10054 等）自动重连并重试一次；

        两次都失败再抛出，由 DataHub 走 akshare 兜底或 fail-closed（绝不落到 tushare）。
        """
        field_list = ["time", "open", "high", "low", "close", "volume", "amount", "preClose"]
        for attempt in range(2):
            try:
                return self.xtdata.get_market_data_ex(
                    field_list=field_list, stock_list=syms, period=period,
                    start_time=s, end_time=e, dividend_type=_ADJUST_MAP[adjust], fill_data=False)
            except Exception as exc:  # noqa: BLE001 - 10054 等连接重置
                logger.warning("QMT get_market_data_ex 第 %d 次失败: %s", attempt + 1, exc)
                if attempt == 0:
                    self._reconnect()
                else:
                    raise
        return None  # 不可达：2 次均失败已在上一轮 raise

    # ------------------------------------------------------------ 下载护栏
    # QMT 的同步下载接口（download_history_data2 / 旧 download_financial_data）
    # 内部是一个 ``while not status[0] and client.is_connected(): sleep(0.1)``
    # 的阻塞等待：只要 TCP 连接还在（行情服务器在线）但数据迟迟不到（常见于
    # 财务/资金流数据在账户上未授权），就会无限空转。本组方法把"阻塞下载"
    # 包进守护线程 + 硬性超时 + 超时取消，杜绝整夜卡死。
    def _run_in_thread(self, fn, timeout: float, label: str):
        """在守护线程跑阻塞调用；超时返回 None（调用方据此降级/重试）。"""
        box: dict = {}
        done = threading.Event()

        def runner():
            try:
                box["r"] = fn()
            except Exception as exc:  # noqa: BLE001
                box["e"] = exc
            finally:
                done.set()

        th = threading.Thread(target=runner, daemon=True)
        th.start()
        if not done.wait(timeout):
            logger.warning("QMT %s 取数线程超时(%ss)，放弃本次取数", label, timeout)
            return None
        if "e" in box:
            logger.warning("QMT %s 取数线程异常: %s", label, box["e"])
            return None
        return box.get("r")

    def _cancel_download(self) -> None:
        """取消可能仍在后台流式拉取的 QMT 下载请求。"""
        try:
            self.xtdata.get_client().stop_supply_history_data2()
        except Exception:  # noqa: BLE001
            pass

    def _on_progress(self, label: str, data: dict) -> None:
        try:
            finished = data.get("finished")
            total = data.get("total")
            if finished is not None and total:
                logger.info("QMT %s 下载进度 %s/%s", label, finished, total)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 历史
    def _refresh_intraday(self, syms: Sequence[str], period: str, end_time: str) -> None:
        """盘中分钟线：请求范围覆盖今日时，按节流间隔增量下载当日段。

        xtdata.get_market_data_ex 只读本地已下载数据，盘中不会自动追加最新分钟，
        不刷新则当日数据停留在上次下载时刻（分时图表现为前段正常、后段缺失）。
        """
        if period == "1d" or not syms:
            return
        today_s = datetime.now().strftime("%Y%m%d")
        if end_time and end_time[:8] < today_s:
            return
        now = time.monotonic()
        for sym in syms:
            key = (sym, period)
            if now - self._last_dl.get(key, 0.0) < _INTRADAY_DL_INTERVAL:
                continue
            try:
                self.xtdata.download_history_data(sym, period,
                                                    start_time=today_s, end_time=today_s)
            except Exception as exc:  # noqa: BLE001 - 下载失败仍按本地存量降级
                logger.warning("QMT 盘中分钟数据增量下载失败 %s: %s", sym, exc)
            self._last_dl[key] = now

    def get_bars(
        self,
        symbols: Sequence[str],
        freq: Freq = Freq.D1,
        start: date | str | None = None,
        end: date | str | None = None,
        adjust: Adjust = Adjust.HFQ,
    ) -> pd.DataFrame:
        syms = [normalize_symbol(s) for s in symbols]
        period = _FREQ_MAP[freq]
        s = str(start).replace("-", "")[:8] if start else ""
        e = str(end).replace("-", "")[:8] if end else ""
        # 分钟线：显式订阅对应周期，把当日分钟数据推入本地缓存
        # （否则 get_market_data_ex 只能读到上次 download_history_data 的陈旧数据）。
        # 仅分钟周期需要订阅推送；日线走 download_history_data，不订阅避免大 universe 无谓开销。
        if period in ("1m", "5m", "15m", "30m", "1h"):
            self.subscribe(syms, period)
        self._refresh_intraday(syms, period, e)
        raw = self._safe_market_data(syms, period, s, e, adjust)
        # xtdata 只读本地已下载数据，本地缺失时会静默返回空。不仅整体为空要补下载，
        # 本地只有短窗口（覆盖不足请求区间）时同样要补，否则长周期分钟线回测
        # 会静默降级成“只读本地存量”（2026-08-16 实测：ETF M5 可下载 242 个交易日，
        # 此前误判为"本地仅约15天"即此缺陷所致）。
        missing = [sym for sym in syms if raw is None or raw.get(sym) is None or len(raw.get(sym)) == 0]
        if s:
            try:
                req_start = pd.to_datetime(s, format="%Y%m%d")
            except ValueError:
                req_start = None
            if req_start is not None:
                for sym in syms:
                    if sym in missing:
                        continue
                    if "time" not in raw[sym].columns:  # 日线无 time 列，跳过覆盖检查
                        continue
                    d0 = pd.to_datetime(raw[sym]["time"], unit="ms", utc=True)
                    d0 = d0.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
                    # 容忍 8 个自然日缺口（节假日/上市日），超出则判定覆盖不足
                    if d0.min() > req_start + pd.Timedelta(days=8):
                        missing.append(sym)
        if missing:
            for sym in missing:
                try:
                    self.xtdata.download_history_data(sym, period, start_time=s, end_time=e)
                except Exception as exc:  # noqa: BLE001 - 下载失败交给降级链
                    logger.warning("QMT 下载历史数据失败 %s: %s", sym, exc)
            raw = self._safe_market_data(syms, period, s, e, adjust)
        frames = []
        for sym, df in (raw or {}).items():
            if df is None or len(df) == 0:
                continue
            df = df.copy()
            df["symbol"] = sym
            if "time" in df.columns:
                # xtdata 的 time 是毫秒 epoch；unit="ms" 默认按 UTC 解析，
                # 需转回北京时间否则分钟时间轴整体偏移 8 小时
                df["date"] = (
                    pd.to_datetime(df["time"], unit="ms", utc=True)
                    .dt.tz_convert("Asia/Shanghai")
                    .dt.tz_localize(None)
                )
            else:
                df["date"] = pd.to_datetime(df.index.astype(str), format="%Y%m%d", errors="coerce")
            df = df.rename(columns={"preClose": "prev_close"})
            if "prev_close" not in df.columns:
                df["prev_close"] = df["close"].shift(1).bfill()
            df["is_suspended"] = df["volume"] <= 0
            # xtdata 的 volume 单位是**手**（1 手=100 股），DataHub 契约为**股**
            # （akshare/tushare 均 ×100 归一）。不归一则换手率/流动性指标整体小 100 倍。
            df["volume"] = df["volume"] * 100.0
            frames.append(
                df[["date", "symbol", "open", "high", "low", "close", "volume", "amount",
                    "prev_close", "is_suspended"]]
            )
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)

    def get_index_bars(
        self, index_symbol: str, start: date | str | None = None, end: date | str | None = None
    ) -> pd.DataFrame:
        return self.get_bars([index_symbol], Freq.D1, start, end, Adjust.NONE)

    # ------------------------------------------------------------ 实时
    def get_realtime(self, symbols: Sequence[str]) -> dict[str, Tick]:
        syms = [normalize_symbol(s) for s in symbols]
        try:
            self.subscribe(syms)
            raw = self.xtdata.get_full_tick(syms) or {}
        except Exception as exc:               # noqa: BLE001 - 订阅/取数异常不应击穿调度链
            logger.warning("QMT 实时行情获取失败: %s", exc)
            return {}
        out: dict[str, Tick] = {}
        for sym, q in raw.items():
            if not q:
                continue
            try:
                timetag = q.get("timetag", 0) or 0
                ts = _parse_timetag(timetag)
                out[sym] = Tick(
                    symbol=sym,
                    time=datetime.fromtimestamp(ts) if ts else datetime.now(),
                    last=float(q.get("lastPrice", 0) or 0),
                    open=float(q.get("open", 0) or 0),
                    high=float(q.get("high", 0) or 0),
                    low=float(q.get("low", 0) or 0),
                    prev_close=float(q.get("lastClose", 0) or 0),
                    # xtdata 实时行情 volume 同样是手 → 股，与 get_bars 口径一致
                    volume=float(q.get("volume", 0) or 0) * 100.0,
                    amount=float(q.get("amount", 0) or 0),
                    bid_prices=tuple(float(x) for x in (q.get("bidPrice") or [])),
                    bid_volumes=tuple(float(x) for x in (q.get("bidVol") or [])),
                    ask_prices=tuple(float(x) for x in (q.get("askPrice") or [])),
                    ask_volumes=tuple(float(x) for x in (q.get("askVol") or [])),
                )
            except Exception as exc:           # noqa: BLE001 - 单只票脏数据跳过
                logger.debug("QMT 实时行情解析 %s 失败: %s", sym, exc)
                continue
        return out

    # ------------------------------------------------------------ 基本面（QMT 财务）
    # Phase 2（2026-08-12）：用 QMT 本地财务数据替代 akshare（用户环境 akshare
    # 基本面/资金流取不到）。xtdata 提供 `download_financial_data` + `get_financial_data`：
    #   - report_type='announce_time'：按**公告日**筛选 —— 天然满足 PIT（公告日之前不可见）
    #   - PershareIndex 表：ROE/EPS/BPS/每股经营现金流/增速/毛利率/负债率 + m_anntime/m_timetag
    #   - Income 表：营收/归母净利润绝对值
    # 注意：QMT 财务数据需先经下载接口落地本地（首次较慢，之后增量）。
    _FIN_TABLES = ("PershareIndex", "Income")
    _FIN_LOOKBACK_DAYS = 3 * 366  # 默认窗口：近 3 年（财务因子用最近几期足够，避免全量下载）

    def get_fundamentals(
        self,
        symbols: Sequence[str],
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> list[Fundamental]:
        if not symbols:
            return []
        syms = [normalize_symbol(s) for s in symbols]
        now = datetime.now()
        s = str(start).replace("-", "")[:8] if start else (
            (now - timedelta(days=self._FIN_LOOKBACK_DAYS)).strftime("%Y%m%d"))
        e = str(end).replace("-", "")[:8] if end else now.strftime("%Y%m%d")
        # 进程内按标的缓存：已解析过的标的直接复用。
        cached = {sym: self._fin_cache[sym] for sym in syms if sym in self._fin_cache}
        missing = [sym for sym in syms if sym not in cached]
        parsed: list[Fundamental] = list(cached.values())
        if missing:
            # 默认窗口（start/end 均为 None，因子 / 预热的标准路径）走跨进程磁盘
            # 缓存，避免每次重跑都重新触发 QMT 全市场下载；显式区间（如回测特定窗口）
            # 不缓存，直接走 QMT 取数。
            if start is None and end is None:
                raw_map = self._ensure_financial_raw(missing, s, e, self._dl_timeout)
            else:
                dl = self._download_raw(missing, self._FIN_TABLES, s, e, self._dl_timeout)
                raw_map = dl or {}
            if raw_map:
                got = self._parse_financial(raw_map, list(raw_map.keys()))
                for f in got:
                    self._fin_cache[f.symbol] = f
                parsed.extend(got)
        if not parsed:
            # 超时/无本地数据：主动跳过本源，让 DataHub 走下一优先级源。
            # 用 SourceSkipped（而非抛普通异常）以避免 QMT 健康度被记失败、
            # 把同样健康的 QMT 行情一并熔断。
            raise SourceSkipped(
                f"QMT 财务下载超时/无本地数据（{self._dl_timeout:.0f}s），交由其它源兜底")
        by_sym = {f.symbol: f for f in parsed}
        out = [by_sym[s] for s in syms if s in by_sym]
        if not out:
            raise SourceSkipped("QMT 财务无匹配标的，交由其它源兜底")
        logger.info("QMT 财务取数完成：%d 条记录（%d 只，近 %s~%s）",
                    len(out), len(set(f.symbol for f in out)), s, e)
        return out

    def prewarm_fundamentals(self, symbols: Sequence[str], timeout: int | None = None) -> int:
        """回测预热：一次性把全 universe 财务下载落地到本地磁盘缓存。

        首次全市场下载可能较慢（数十秒~数分钟），集中在这里并给足超时；
        之后每个调仓日 / 每次重跑走磁盘缓存 + 进程内 _fin_cache，秒回。
        返回落地标的数。
        """
        if not symbols:
            return 0
        syms = [normalize_symbol(s) for s in symbols]
        now = datetime.now()
        s = (now - timedelta(days=self._FIN_LOOKBACK_DAYS)).strftime("%Y%m%d")
        e = now.strftime("%Y%m%d")
        t = float(timeout) if timeout else max(self._dl_timeout, len(syms) * 3)
        raw_map = self._ensure_financial_raw(syms, s, e, t)
        got = self._parse_financial(raw_map, list(raw_map.keys()))
        for f in got:
            self._fin_cache[f.symbol] = f
        return len(got)

    # ──────────────────────────────────────────────────────────── 财务磁盘缓存
    # 痛点：进程内缓存（_fin_cache）每次新开 python 进程即清空，导致重跑回测 /
    # walk_forward --include-extra 都重新触发 download_financial_data2（全市场
    # 10326 只从头扫一遍）。这里加一层**跨进程磁盘缓存**：原始财务表
    # （PershareIndex / Income）按标的落 parquet，TTL 内直接读盘、跳过 QMT 下载；
    # 仅缺失 / 过期标的才走 QMT（其本身也是增量下载）。财务为不可变历史，TTL 默认
    # 1 天（对齐 datahub.cache.fundamental_ttl）——既"下载一次复用"，又能在新
    # 财报发布后自然刷新。保留原始多期数据（而非仅最新一期）以保证 PIT 切片正确。
    def _fin_dir(self) -> Path:
        d = Path(self._fin_cache_dir) if self._fin_cache_dir else (
            _PROJ_ROOT / "data" / "fundamentals")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _fin_path(self, sym: str, table: str) -> Path:
        safe = sym.replace(".", "_")  # 文件名更友好（规避 Windows 保留字符）
        return self._fin_dir() / f"{safe}__{table}.parquet"

    def _fin_fresh(self, sym: str) -> bool:
        """磁盘缓存齐全且未过期（TTL 内）则返回 True。"""
        if not self._fin_ttl:
            return False  # ttl=0 → 视作不缓存，每次走 QMT
        for t in self._FIN_TABLES:
            p = self._fin_path(sym, t)
            if not p.exists():
                return False
            if (time.time() - p.stat().st_mtime) > self._fin_ttl:
                return False
        return True

    def _load_symbol_raw(self, sym: str) -> dict | None:
        if not self._fin_fresh(sym):
            return None
        raw: dict = {}
        for t in self._FIN_TABLES:
            try:
                df = pd.read_parquet(self._fin_path(sym, t))
            except Exception:  # noqa: BLE001
                return None
            if df is not None and not getattr(df, "empty", True):
                raw[t] = df
        return raw or None

    def _save_symbol_raw(self, sym: str, raw: dict) -> None:
        for t, df in raw.items():
            if df is None or getattr(df, "empty", True):
                continue
            try:
                df.to_parquet(self._fin_path(sym, t), index=False)
            except Exception as exc:  # noqa: BLE001 - 单票写盘失败不影响整体
                logger.debug("财务缓存写盘失败 %s/%s: %s", sym, t, exc)

    def _download_raw(
        self,
        syms: list[str],
        tables: Sequence[str],
        s: str,
        e: str,
        timeout: int = 180,
    ) -> dict[str, dict] | None:
        """异步触发 QMT 财务下载并轮询本地落地，返回 {sym:{table:df}}；超时返回 None。"""
        tables = list(tables)
        # 大 universe 首次下载给足时间：按标的数线性放大（200 只≈600s），
        # 杜绝"首次全量下载被 180s 砍掉→ 永久降级 akshare"。护栏仍会取消+降级，不会卡死。
        eff_timeout = max(float(timeout), len(syms) * 3)

        def worker() -> dict[str, dict] | None:
            try:
                self.xtdata.download_financial_data2(
                    syms, tables, s, e, lambda d: self._on_progress("财务", d))
            except Exception as exc:  # noqa: BLE001
                logger.warning("QMT 财务下载请求发起失败（仍尝试读本地缓存）: %s", exc)
            deadline = time.monotonic() + eff_timeout
            while time.monotonic() < deadline:
                try:
                    raw = self.xtdata.get_financial_data(syms, tables, s, e, "announce_time")
                except Exception:  # noqa: BLE001
                    raw = {}
                if self._financial_has_data(raw, tables):
                    out: dict[str, dict] = {}
                    for sym in syms:
                        sd = (raw or {}).get(sym) or {}
                        part = {t: sd[t] for t in tables
                                if sd.get(t) is not None
                                and not getattr(sd.get(t), "empty", True)}
                        if part:
                            out[sym] = part
                    return out or None
                time.sleep(0.5)
            return None

        parsed = self._run_in_thread(worker, eff_timeout + 5, "财务")
        if parsed is None:
            self._cancel_download()
        return parsed

    def _ensure_financial_raw(
        self,
        syms: list[str],
        s: str,
        e: str,
        timeout: int = 180,
    ) -> dict[str, dict]:
        """返回 {sym:{table:df}}：优先磁盘缓存，缺失 / 过期才走 QMT 下载并落盘。"""
        out: dict[str, dict] = {}
        to_fetch: list[str] = []
        for sym in syms:
            cached = self._load_symbol_raw(sym)
            if cached is not None:
                out[sym] = cached
            else:
                to_fetch.append(sym)
        if to_fetch:
            logger.info("财务磁盘缓存命中 %d/%d，QMT 需下载 %d 只",
                        len(out), len(syms), len(to_fetch))
            dl = self._download_raw(to_fetch, self._FIN_TABLES, s, e, timeout)
            if dl:
                for sym, raw_sym in dl.items():
                    out[sym] = raw_sym
                    self._save_symbol_raw(sym, raw_sym)
        return out

    @staticmethod
    def _financial_has_data(raw: dict, tables: Sequence[str]) -> bool:
        for sym_d in (raw or {}).values():
            for t in tables:
                df = (sym_d or {}).get(t)
                if df is not None and not getattr(df, "empty", True):
                    return True
        return False

    def _parse_financial(self, raw: dict, syms: list[str]) -> list[Fundamental]:
        def _dt(v) -> date | None:
            try:
                return datetime.strptime(str(v).strip(), "%Y%m%d").date()
            except Exception:  # noqa: BLE001
                return None

        out: list[Fundamental] = []
        for sym in syms:
            per = (raw.get(sym) or {}).get("PershareIndex")
            inc = (raw.get(sym) or {}).get("Income")
            if per is None or per.empty:
                continue
            per = per.sort_values("m_anntime")  # 公告日升序 → 取最新一条
            last = per.iloc[-1]
            rec = last.to_dict() if hasattr(last, "to_dict") else dict(last)
            ann = _dt(rec.get("m_anntime") or "")
            period = _dt(rec.get("m_timetag") or "")
            if ann is None or period is None:
                continue  # 无公告日/报告期 → 丢弃（宁可少数据不冒险）
            # Income 表按报告期匹配营收/净利润（同季度）
            revenue = net_profit = float("nan")
            if inc is not None and not inc.empty:
                try:
                    ic = inc.sort_values("m_timetag")
                    ic["_pd"] = ic["m_timetag"].map(_dt)
                    row = ic[ic["_pd"] == period]
                    if not row.empty:
                        revenue = _num(row.iloc[0].get("revenue_inc"))
                        net_profit = _num(row.iloc[0].get("net_profit_excl_min_int_inc"))
                except Exception:  # noqa: BLE001
                    pass
            out.append(Fundamental(
                symbol=sym,
                ann_date=ann,
                report_period=period,
                revenue=revenue,
                net_profit=net_profit,
                roe=_num(rec.get("du_return_on_equity")),          # 净资产收益率
                revenue_yoy=_num(rec.get("inc_revenue_rate")),      # 营收同比
                profit_yoy=_num(rec.get("inc_net_profit_rate")
                                 or rec.get("du_profit_rate")),     # 归母净利润同比
                gross_margin=_num(rec.get("sales_gross_profit")
                                   or rec.get("gross_profit")),     # 销售毛利率
                debt_ratio=_num(rec.get("gear_ratio")),             # 资产负债率
                ocf=_num(rec.get("s_fa_ocfps")),                    # 每股经营现金流
                eps=_num(rec.get("s_fa_eps_basic")),                # 基本每股收益
                bps=_num(rec.get("s_fa_bps")),                      # 每股净资产
            ))
        return out

    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[InstrumentInfo]:
        syms = [normalize_symbol(s) for s in (symbols or [])]
        if not syms:
            syms = list(self.xtdata.get_stock_list_in_sector("沪深A股") or [])
        out: list[InstrumentInfo] = []
        for sym in syms:
            detail = self.xtdata.get_instrument_detail(sym) or {}
            name = str(detail.get("InstrumentName", ""))
            open_date = str(detail.get("OpenDate", "") or "")
            list_date = None
            if len(open_date) == 8 and open_date.isdigit():
                list_date = datetime.strptime(open_date, "%Y%m%d").date()
            out.append(
                InstrumentInfo(
                    symbol=sym,
                    name=name,
                    list_date=list_date,
                    is_st="ST" in name.upper(),
                    total_share=float(detail.get("TotalVolume", 0) or 0),
                    float_share=float(detail.get("FloatVolume", 0) or 0),
                    extra={"pre_close": detail.get("PreClose")},
                )
            )
        return out

    # ------------------------------------------------------------ 资金流（QMT）
    # Phase 2（2026-08-12）：用 QMT「逐笔成交统计」替代 akshare 个股资金流。
    #   - period="transactioncount1d"：Level1 逐笔成交统计（日级），字段风格同
    #     l2transactioncount 附录（netInflowMostAmount 超大单净额 / netInflowBigAmount
    #     大单净额 / ...）；先 download_history_data2 落地本地再读取。
    #   - 标准化输出：symbol / date / net_inflow（主力净额=超大单+大单）/
    #     big_order_ratio（主力净占比=net_inflow/成交额），供 moneyflow 因子消费。
    # 注意：Level1 与 Level2 的实际列名可能有差异，用候选列名容错，运行时以
    # scripts/diag_qmt_fundamentals.py --moneyflow 打印的真实列名为准。
    # 用户实测示例：field_list=['large_net_inflow'] 即为大单/主力净流入字段。
    _MAIN_NET_COLS = ("main_net_inflow", "mainNetInflow", "large_net_inflow")
    _MOST_NET_COLS = ("netInflowMostAmount", "netInflowMost", "mainNetInflow")
    _BIG_NET_COLS = ("netInflowBigAmount", "netInflowBig", "bigNetInflow",
                     "large_net_inflow")

    def get_money_flow(
        self,
        symbols: Sequence[str],
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()
        syms = [normalize_symbol(s) for s in symbols]
        s = str(start).replace("-", "")[:8] if start else "20000101"
        e = str(end).replace("-", "")[:8] if end else datetime.now().strftime("%Y%m%d")

        def worker() -> dict | None:
            # download_history_data2 内部有阻塞等待循环 → 必须在守护线程里跑，
            # 超时由 _run_in_thread 兜住；这里再轮询本地落地确保读到数据。
            try:
                self.xtdata.download_history_data2(
                    syms, "transactioncount1d", s, e, lambda d: self._on_progress("资金流", d))
            except Exception as exc:  # noqa: BLE001
                logger.warning("QMT 资金流下载请求失败: %s", exc)
                return None
            deadline = time.monotonic() + self._dl_timeout
            while time.monotonic() < deadline:
                try:
                    raw = self.xtdata.get_market_data_ex(
                        field_list=[], stock_list=syms, period="transactioncount1d",
                        start_time=s, end_time=e, dividend_type="none", fill_data=False,
                    )
                except Exception:  # noqa: BLE001
                    raw = {}
                if raw and any(df is not None and len(df) for df in raw.values()):
                    return raw
                time.sleep(0.5)
            return None

        raw = self._run_in_thread(worker, self._dl_timeout + 5, "资金流")
        if not raw:
            return pd.DataFrame()

        def _pick_col(cols, candidates) -> str | None:
            for c in candidates:
                if c in cols:
                    return c
            return None

        frames = []
        for sym, df in (raw or {}).items():
            if df is None or len(df) == 0:
                continue
            df = df.copy()
            cols = list(df.columns)
            # 主力净额：优先直接用"主力/大单净流入"整体字段；否则拆分超大单+大单
            main = _pick_col(cols, self._MAIN_NET_COLS)
            if main is not None:
                net = df[main].fillna(0).astype(float)
            else:
                most = _pick_col(cols, self._MOST_NET_COLS)
                big = _pick_col(cols, self._BIG_NET_COLS)
                if most is None and big is None:
                    logger.debug("QMT 资金流 %s 无净流入列（列=%s）", sym, cols[:10])
                    continue
                net = df[most].fillna(0).astype(float) if most is not None else 0.0
                if big is not None:
                    net = net + df[big].fillna(0).astype(float)
            # date：transactioncount1d 的索引/时间列
            if "time" in cols:
                dates = (pd.to_datetime(df["time"], unit="ms", utc=True)
                         .dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.date)
            elif "date" in cols:
                dates = pd.to_datetime(df["date"]).dt.date
            else:
                dates = pd.to_datetime(df.index.astype(str), format="%Y%m%d",
                                       errors="coerce").date
            amount_col = _pick_col(cols, ("amount", "AMOUNT", "totalAmount"))
            amount = df[amount_col].fillna(0) if amount_col is not None else 0.0
            out = pd.DataFrame({
                "symbol": sym,
                "date": pd.to_datetime(list(dates)),
                "net_inflow": net.astype(float),
                "big_order_ratio": (net / amount.replace(0, float("nan"))).astype(float)
                                   if amount_col is not None else float("nan"),
            })
            frames.append(out)
        if not frames:
            return pd.DataFrame()
        return (pd.concat(frames, ignore_index=True)
                .sort_values(["symbol", "date"]).reset_index(drop=True))
