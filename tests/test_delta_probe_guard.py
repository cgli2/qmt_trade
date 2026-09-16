"""日线增量探测的收敛/负缓存（性能）+ 盘中当日 bar 刷新（数据正确性）回归测试（2026-09-16）。

根因：K线图页面 /market/kline 恒传 end=今天，而缓存只覆盖到上一交易日，
manager.get_bars 的增量补齐分支因此**每次请求都成立**，向数据源探测
[cached_end+1, 今天] 这段盘前注定无数据的区间 —— qmt 快速返回空后降级到
akshare 兜底，而 akshare 无论区间多窄都是全量 HTTP 拉取（实测单次 6.98s），
失败结果还不被记忆。实测：连续切换 6 只个股首次合计 30.5s（平均 5.1s/只）。

两层修复：
  1. **探测区间收敛**：仅在 CLOSED/PRE_OPEN（09:15 开盘竞价前、非交易日）把上界
     压到上一交易日 —— 这两个时段当日日线**绝对不可能存在**，探测必失败。
     收敛后区间为空 → 一次数据源都不碰。
  2. **探测负缓存**：收敛后仍需探测但失败的区间，冷却窗内不再重试。
     key 不含 symbol（失败根因与标的无关），含 delta_start/probe_end（避免
     宽区间被窄区间的失败误伤）。

★ 层 1 的边界**必须**只到 09:15。第一版曾按「非 POST_CLOSE 一律排除当日」收敛，
盘中把正在形成的当日 bar 一并挡掉，行情页会整日冻结在昨收直到 24h 内存 TTL 过期。
真实数据源实测（2026-09-16 09:40 MORNING，001256.SZ）已证伪第一版：
    qmt     dates=['2026-09-15','2026-09-16']  当日bar=YES  0.89s
    akshare dates=['2026-09-15','2026-09-16']  当日bar=YES  6.98s
故 test_intraday_probes_today_bar / test_lunch_and_auction_do_not_clamp 为回归守护。

同一实测也定位了 6s 的归属：盘前 akshare 兜底一次 HTTP ~7s（区间再窄也是全量拉取），
与盘前实测 6.02s/请求吻合；盘中 qmt 首优命中 0.89s（本地命中后 0.02s），无延迟。
HTTP 端到端同脚本对比（修复前代码）：盘前连切 6 只合计 30.52s，盘中仅 0.28s。

★ 数据正确性修复 case-B（2026-09-16）：盘中「当日 bar」被范围缓存冻结。
层 1/2 解决盘前"探测注定失败"的性能问题后，暴露一处数据正确性缺陷：日线范围
缓存 key **不含 end**，盘中一旦写入当日 bar，增量分支前置条件 cached_end < end
恒为假 → 永不重探 → 当日那根 bar 被**冻结在首次取数时刻**（实测 14:39 的 K 线仍
显示 09:40 的 close=21.23 / vol=1300 手，而实时价已 21.54 / vol=15328 手，偏差
1.4%，MA5/10/20/60 全跟着错）。
修复：_today_refresh_due + _today_refresh_at 按 today_bar_refresh_ttl(默认 60s)
主动重探 [今天,今天]、drop_duplicates(keep="last") 让新值覆盖冻结 bar、
_tail_fingerprint(close/volume 之和)变化才写盘（避免每分钟无谓重写争 DuckDB 单写
锁）；历史 end（回测）第一道判据 end.date()!=today 即 no-op，PIT 安全。配套 t1：
首次下载已含当日 bar 时记一次刷新时刻，抑制紧接着对 [今天,今天] 的冗余重探。
守护测试见 section 2c（IntradayTodayProvider 让当日 bar 值可变，才能证明"刷新把
冻结值覆盖成新值"）；反向验证 tests/diag_caseb_reverse.py 猴补 _today_refresh_due
→恒 False 模拟撤销：核心/节流/写穿三条变红、历史 end no-op 保持绿。

★ 端到端用例一律用 _frozen_now() 冻结时钟，并**显式断言「增量分支的前置条件
成立」**（cached_end < end）。缺了这条断言，测试会因为根本没走进 buggy 分支而
平凡通过 —— 本项目第一版测试正是这样漏掉的（还原修复后仍然全绿）。

用内存 MarketRepository + 合成数据源直接驱动 DataHub，不触碰生产 qmt.duckdb。

运行：pytest tests/test_delta_probe_guard.py  或  python tests/test_delta_probe_guard.py
（遵守项目规则：所有测试用 Python 脚本，禁用 curl。）
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qmt_trade.datahub.manager as mgr  # noqa: E402
from qmt_trade.core.clock import AUCTION_OPEN_START, TradingCalendar  # noqa: E402
from qmt_trade.core.config import Settings  # noqa: E402
from qmt_trade.datahub.manager import DataHub  # noqa: E402
from qmt_trade.datahub.providers.base import Capability, DataProvider  # noqa: E402
from qmt_trade.datahub.types import Adjust, Freq  # noqa: E402
from qmt_trade.storage.db import Database  # noqa: E402
from qmt_trade.storage.market import MarketRepository  # noqa: E402

S1, S2 = "600000.SH", "000001.SZ"

CAL = TradingCalendar()

# 固定时钟样本（2026-09-16 是周三交易日；2026-10-01 是内置法定节假日）
NOW_PREMARKET = datetime(2026, 9, 16, 8, 49)     # PRE_OPEN      → 收敛到 09-15
NOW_EDGE_914 = datetime(2026, 9, 16, 9, 14)      # PRE_OPEN 末刻 → 收敛到 09-15
NOW_AUCTION = datetime(2026, 9, 16, 9, 15)       # AUCTION_OPEN  → 不收敛（09-16）
NOW_MORNING = datetime(2026, 9, 16, 10, 30)      # MORNING       → 不收敛
NOW_LUNCH = datetime(2026, 9, 16, 12, 0)         # LUNCH         → 不收敛
NOW_AFTERNOON = datetime(2026, 9, 16, 14, 30)    # AFTERNOON     → 不收敛
NOW_POSTCLOSE = datetime(2026, 9, 16, 15, 30)    # POST_CLOSE    → 不收敛
NOW_SATURDAY = datetime(2026, 9, 19, 10, 0)      # CLOSED 周末   → 收敛到 09-18(周五)
NOW_HOLIDAY = datetime(2026, 10, 1, 10, 0)       # CLOSED 国庆   → 收敛到 09-30(周三)


# ------------------------------------------------------------------ 时钟冻结
class _FixedDateTime(datetime):
    """替换 manager.datetime，让 datetime.now() 返回测试设定的固定时刻。"""

    _fixed: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        if cls._fixed is None:
            return datetime.now(tz)
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


@contextmanager
def _frozen_now(moment: datetime):
    """冻结 manager 模块的当前时间（只影响 _clamp_probe_end，用后必还原）。"""
    orig = mgr.datetime
    _FixedDateTime._fixed = moment
    mgr.datetime = _FixedDateTime
    try:
        yield
    finally:
        mgr.datetime = orig
        _FixedDateTime._fixed = None


# ------------------------------------------------------------------ 合成数据源
def _synth_bars(symbol: str, start, end) -> pd.DataFrame:
    """确定性生成 [start, end] 的日线（工作日）。同一 symbol 永远同一序列。"""
    s = pd.Timestamp(start) if start else pd.Timestamp("2026-01-01")
    e = pd.Timestamp(end) if end else pd.Timestamp("2026-12-31")
    dates = pd.bdate_range(s, e)
    n = len(dates)
    if n == 0:
        return pd.DataFrame()
    seed = int(hashlib.md5(symbol.encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    base = 10.0 + (seed % 50)
    close = np.maximum(base + np.cumsum(rng.normal(0, 0.05, n)), 1.0)
    open_ = np.concatenate([[base], close[:-1]])
    return pd.DataFrame({
        "date": dates,
        "symbol": symbol,
        "open": open_.round(2),
        "high": (np.maximum(open_, close) * 1.01).round(2),
        "low": (np.minimum(open_, close) * 0.99).round(2),
        "close": close.round(2),
        "volume": rng.integers(100_000, 2_000_000, n).astype(float),
    })


class ProbeCountingProvider(DataProvider):
    """name 不含 'mock' → DataHub 磁盘缓存对其启用。

    ``data_until`` 之后的区间一律返回空，模拟「数据源尚未发布当日日线」；
    每次 get_bars 都记录 (start, end)，供断言增量探测是否真的被发起。
    """

    name = "synthetic"
    capabilities = {Capability.BARS}

    def __init__(self, data_until: date | None = None, **kw):
        super().__init__(**kw)
        self.data_until = data_until
        self.calls: list[tuple[str | None, str | None]] = []

    @property
    def data_until(self) -> pd.Timestamp | None:
        return self._data_until

    @data_until.setter
    def data_until(self, value) -> None:
        """统一归一化为 Timestamp。用例中途改 data_until 模拟「数据源刚发布当日
        日线」，若直接存 date 就会在 s > data_until 处抛 Timestamp/date 比较错。"""
        self._data_until = pd.Timestamp(value) if value is not None else None

    def is_available(self) -> bool:
        return True

    def get_bars(self, symbols, freq=Freq.D1, start=None, end=None, adjust=Adjust.HFQ):
        s = pd.Timestamp(start) if start is not None else None
        e = pd.Timestamp(end) if end is not None else None
        self.calls.append((s.date().isoformat() if s is not None else None,
                           e.date().isoformat() if e is not None else None))
        until = self._data_until
        if s is not None and until is not None and s > until:
            return pd.DataFrame()          # 缺口区间无数据 → _dispatch 判空 → 降级链耗尽
        eff_end = e
        if until is not None:
            eff_end = min(e, until) if e is not None else until
        frames = [_synth_bars(sym, s, eff_end) for sym in symbols]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame()
        return (pd.concat(frames, ignore_index=True)
                .sort_values(["symbol", "date"]).reset_index(drop=True))


class IntradayTodayProvider(DataProvider):
    """盘中数据源：历史序列确定，但**当日 bar 的收盘价/量可变**（模拟盘中持续刷新）。

    case-B 守护专用。ProbeCountingProvider 的当日 bar 值恒定（来自 _synth_bars），
    无法证明「重探把冻结的当日 bar 更新成了新值」；本 provider 允许测试在两次
    get_bars 之间改 today_close/today_vol，从而验证刷新确实覆盖了旧值。

    name='synthetic' → 与 ProbeCountingProvider 同 priority 配置，磁盘缓存对其启用。
    """

    name = "synthetic"
    capabilities = {Capability.BARS}

    def __init__(self, today: date, today_close: float = 100.0,
                 today_vol: float = 1_000.0, **kw):
        super().__init__(**kw)
        self.today = pd.Timestamp(today)
        self.today_close = today_close
        self.today_vol = today_vol
        self.calls: list[tuple[str | None, str | None]] = []

    def is_available(self) -> bool:
        return True

    def get_bars(self, symbols, freq=Freq.D1, start=None, end=None, adjust=Adjust.HFQ):
        s = pd.Timestamp(start) if start is not None else None
        e = pd.Timestamp(end) if end is not None else None
        self.calls.append((s.date().isoformat() if s is not None else None,
                           e.date().isoformat() if e is not None else None))
        frames = []
        for sym in symbols:
            # 历史：严格早于 today 的确定性序列（不含当日）
            hist = _synth_bars(sym, s, self.today - pd.Timedelta(days=1))
            if not hist.empty:
                hist = hist[pd.to_datetime(hist["date"]) < self.today]
                if not hist.empty:
                    frames.append(hist)
            # 当日 bar：仅当请求区间覆盖 today 才给（值取当前 today_close/today_vol）
            covers_today = ((s is None or s <= self.today)
                            and (e is None or e >= self.today))
            if covers_today:
                frames.append(pd.DataFrame([{
                    "date": self.today, "symbol": sym,
                    "open": self.today_close,
                    "high": round(self.today_close * 1.01, 2),
                    "low": round(self.today_close * 0.99, 2),
                    "close": self.today_close,
                    "volume": self.today_vol,
                }]))
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame()
        return (pd.concat(frames, ignore_index=True)
                .sort_values(["symbol", "date"]).reset_index(drop=True))


def _make_hub(tmp: str, provider: DataProvider, cooldown: float = 60,
              today_ttl: float = 60) -> DataHub:
    """隔离 DataHub：data_dir 指向临时目录，store 用内存库。

    :param cooldown: 层 2 负缓存冷却秒数（delta_probe_cooldown）。
    :param today_ttl: case-B 当日 bar 刷新 TTL（today_bar_refresh_ttl）；<=0 关闭刷新。
    """
    st = Settings({
        "app": {"data_dir": tmp},
        "datahub": {
            "priority": {"bars": ["synthetic"]},
            "circuit_breaker": {"fail_threshold": 10, "cooldown_seconds": 300},
            "cache": {"max_items": 4096, "daily_bar_ttl": 86400,
                      "delta_probe_cooldown": cooldown,
                      "today_bar_refresh_ttl": today_ttl},
            "quality": {"max_missing_ratio": 0.2, "max_abs_return": 0.35},
        },
    }, env_overlay=False)
    store = MarketRepository(Database(":memory:", schema="market"))
    return DataHub(st, [provider], store=store)


# ============================================== 1. 收敛上界（纯函数，注入时钟）
def test_clamp_is_noop_for_historical_end():
    """回测安全性：end 落在过去时必须原样返回，绝不改变回测取数语义。"""
    with tempfile.TemporaryDirectory() as tmp:
        hub = _make_hub(tmp, ProbeCountingProvider())
        with _frozen_now(NOW_PREMARKET):
            for hist in ("2026-01-09", "2025-06-30", "2024-02-09", "2026-09-15"):
                got = hub._clamp_probe_end(date.fromisoformat(hist))
                assert got == date.fromisoformat(hist), \
                    f"历史 end={hist} 应原样返回，实际 {got}"


def test_clamp_by_session():
    """各时段收敛结果：只有盘前(<09:15)与非交易日排除当日，其余一律含当日。

    ★ 09:15 边界两侧必须都有样本。第一版收敛把午休/上午盘/下午盘也一并排除当日，
    真实数据源实测已证伪（盘中当日 bar 确实存在），这里把它钉成回归守护。
    """
    cases = [
        (NOW_PREMARKET, date(2026, 9, 15), "交易日盘前 08:49"),
        (NOW_EDGE_914, date(2026, 9, 15), "交易日盘前末刻 09:14"),
        (NOW_AUCTION, date(2026, 9, 16), "开盘集合竞价 09:15"),
        (NOW_MORNING, date(2026, 9, 16), "上午盘 10:30"),
        (NOW_LUNCH, date(2026, 9, 16), "交易日午休 12:00"),
        (NOW_AFTERNOON, date(2026, 9, 16), "下午盘 14:30"),
        (NOW_POSTCLOSE, date(2026, 9, 16), "交易日收盘后 15:30"),
        (NOW_SATURDAY, date(2026, 9, 18), "周六"),
        (NOW_HOLIDAY, date(2026, 9, 30), "国庆法定节假日"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        hub = _make_hub(tmp, ProbeCountingProvider())
        for now, expect, label in cases:
            with _frozen_now(now):
                got = hub._clamp_probe_end(now.date())
                assert got == expect, f"{label}（{now}）应收敛到 {expect}，实际 {got}"
                assert got <= now.date(), f"{label}：收敛后上界不得超过请求 end"


def test_clamp_matches_calendar_oracle():
    """收敛结果必须与「当日 bar 是否可能存在」的独立推算一致（防实现漂移）。

    oracle 不复用 _NO_TODAY_BAR_SESSIONS / session_of，而是直接从业务事实推：
    当日日线只可能在「今天是交易日」且「已过 09:15 开盘集合竞价起点」之后出现，
    否则上界只能是上一个交易日。与被测实现走的是 clock.py 的不同代码路径。
    """
    samples = (NOW_PREMARKET, NOW_EDGE_914, NOW_AUCTION, NOW_MORNING, NOW_LUNCH,
               NOW_AFTERNOON, NOW_POSTCLOSE, NOW_SATURDAY, NOW_HOLIDAY)
    with tempfile.TemporaryDirectory() as tmp:
        hub = _make_hub(tmp, ProbeCountingProvider())
        for now in samples:
            d = now.date()
            if not CAL.is_trading_day(d) or now.time() < AUCTION_OPEN_START:
                oracle = CAL.prev_trading_day(d)      # 当日无 bar → 回退到上一交易日
            else:
                oracle = d                            # 当日 bar 可能存在 → 不收敛
            with _frozen_now(now):
                assert hub._clamp_probe_end(d) == oracle, (
                    f"{now}（{CAL.session_of(now).name}）与独立推算不一致："
                    f"实现={hub._clamp_probe_end(d)} oracle={oracle}")


# ============================ 2. 核心：缓存已覆盖最近收盘日时，切股零增量探测
def _assert_delta_branch_reachable(df: pd.DataFrame, end: date, label: str) -> None:
    """断言「增量分支前置条件成立」：cached_end < end。

    没有这条断言，测试可能因为压根没走进 buggy 分支而平凡通过。
    """
    assert not df.empty, f"{label}：首次取数应有数据"
    cached_end = pd.to_datetime(df["date"]).max().date()
    assert cached_end < end, (
        f"{label}：用例失效 —— 缓存末端 {cached_end} 未早于请求 end {end}，"
        f"增量补齐分支根本不会触发，本测试无法覆盖 bug")


def test_no_delta_probe_premarket():
    """复现用户场景：盘前点个股，缓存只到上一交易日 → 一次增量探测都不发。

    **只隔离验证层 1（日历收敛）**，故 cooldown=0 关掉层 2（负缓存）。
    反向验证已证实：若留着负缓存，撤掉层 1 后本用例**依然通过** —— 因为首次
    调用内部那次失败探测会被记入冷却窗，后续 6 次点击被层 2 压制，从而掩盖
    层 1 的缺失。因此这里必须断言**绝对调用序列**，而不是「调用数没有增长」。
    """
    with tempfile.TemporaryDirectory() as tmp, _frozen_now(NOW_PREMARKET):
        end = NOW_PREMARKET.date()                    # 2026-09-16，前端恒传 today
        data_until = date(2026, 9, 15)                # 数据源只发布到上一交易日
        prov = ProbeCountingProvider(data_until=data_until)
        hub = _make_hub(tmp, prov, cooldown=0)

        start = end - timedelta(days=30)
        df1 = hub.get_bars([S1], Freq.D1, start, end, validate=False)
        _assert_delta_branch_reachable(df1, end, "盘前")
        assert pd.to_datetime(df1["date"]).max().date() == data_until
        # 首次调用只应有「一次全量下载」，绝不能附带 [09-16, 09-16] 的缺口探测
        assert prov.calls == [(start.isoformat(), end.isoformat())], \
            f"盘前首次取数不应发起任何增量探测，实际调用 {prov.calls}"

        # 连点 6 次（模拟用户切换个股），全部命中缓存、零探测
        for _ in range(6):
            df = hub.get_bars([S1], Freq.D1, start, end, validate=False)
            assert len(df) == len(df1), "收敛后返回行数应与首次一致（不得丢数据）"
        assert prov.calls == [(start.isoformat(), end.isoformat())], \
            f"盘前缓存已覆盖最近收盘日，连点 6 次也不应新增任何数据源调用；实际 {prov.calls}"


def test_no_delta_probe_across_symbols_premarket():
    """切换不同标的同样零探测（收敛与负缓存都与 symbol 无关）。

    同样 cooldown=0 隔离层 1：断言「每标的恰好一次全量下载」的绝对调用数。
    """
    with tempfile.TemporaryDirectory() as tmp, _frozen_now(NOW_PREMARKET):
        end = NOW_PREMARKET.date()
        prov = ProbeCountingProvider(data_until=date(2026, 9, 15))
        hub = _make_hub(tmp, prov, cooldown=0)

        for sym in (S1, S2, S1, S2):
            df = hub.get_bars([sym], Freq.D1, end - timedelta(days=30), end, validate=False)
            _assert_delta_branch_reachable(df, end, f"盘前/{sym}")
        # 只有 2 次首次全量下载（S1/S2 各一次），无任何增量探测
        assert len(prov.calls) == 2, f"应只有 2 次全量下载，实际 {prov.calls}"
        assert all(c[0] == (end - timedelta(days=30)).isoformat() for c in prov.calls), \
            f"全部调用都应是全量下载（start=区间起点），出现了窄区间探测: {prov.calls}"


def test_postclose_still_probes_when_data_published():
    """收盘后数据源已发布当日日线 → 必须照常探测并补齐（修复不得挡住正常增量）。

    **只隔离验证层 1（日历收敛）**，故 cooldown=0 关掉层 2（负缓存）。
    实测确认：POST_CLOSE 时 _clamp_probe_end(09-16) == 09-16，收敛不排除当日；
    若不关负缓存，本用例第一阶段的探测失败会写入冷却标记、把第二阶段压制掉
    （那是层 2 的既定行为，由 test_probe_success_clears_negative_entry 覆盖），
    两层混在一起会让本测试失去定位能力。
    """
    with tempfile.TemporaryDirectory() as tmp, _frozen_now(NOW_POSTCLOSE):
        end = NOW_POSTCLOSE.date()                    # 2026-09-16
        prov = ProbeCountingProvider(data_until=end)   # 数据源已有当日数据
        hub = _make_hub(tmp, prov, cooldown=0)

        # 先造一份只到 09-15 的缓存
        prov.data_until = date(2026, 9, 15)
        df0 = hub.get_bars([S1], Freq.D1, end - timedelta(days=30), end, validate=False)
        _assert_delta_branch_reachable(df0, end, "收盘后/造缓存")

        # 数据源发布当日数据后，下一次请求应补齐到 09-16
        prov.data_until = end
        n_before = len(prov.calls)
        df = hub.get_bars([S1], Freq.D1, end - timedelta(days=30), end, validate=False)
        assert len(prov.calls) == n_before + 1, \
            f"收盘后应照常发起增量探测，实际调用 {prov.calls}"
        assert prov.calls[-1] == (end.isoformat(), end.isoformat()), \
            f"探测区间应为当日缺口 [{end}, {end}]，实际 {prov.calls[-1]}"
        assert pd.to_datetime(df["date"]).max().date() == end, \
            f"收盘后数据已发布，应补齐到 {end}，实际末端 {pd.to_datetime(df['date']).max()}"


# ================== 2b. 回归守护：盘中/午休/竞价**不得**收敛掉当日正在形成的 bar
def test_lunch_and_auction_do_not_clamp():
    """纯函数层：09:15 起到收盘后，探测上界必须原样保留当日。

    第一版实现按「非 POST_CLOSE 一律排除当日」收敛，把 10:30/12:00/14:30 的当日
    bar 全部挡掉 → 行情页整日冻结在昨收，直到 24h 内存 TTL 过期。本用例即那道红线。
    """
    intraday = [
        (NOW_AUCTION, "开盘集合竞价 09:15"),
        (NOW_MORNING, "上午盘 10:30"),
        (NOW_LUNCH, "午休 12:00"),
        (NOW_AFTERNOON, "下午盘 14:30"),
        (NOW_POSTCLOSE, "收盘后 15:30"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        hub = _make_hub(tmp, ProbeCountingProvider())
        for now, label in intraday:
            with _frozen_now(now):
                got = hub._clamp_probe_end(now.date())
                assert got == now.date(), (
                    f"{label}（{CAL.session_of(now).name}）：当日 bar 可能存在，"
                    f"探测上界必须保留 {now.date()}，实际被收敛到 {got}")
            # 双向不变式：只有 _NO_TODAY_BAR_SESSIONS 里的时段才允许收敛掉当日
            assert (CAL.session_of(now) in DataHub._NO_TODAY_BAR_SESSIONS) is False, \
                f"{label} 不应被列入「当日绝无 bar」的时段集合"


def test_intraday_probes_today_bar():
    """端到端层：盘中数据源已给出当日 bar → 必须真的探测并补齐到当日。

    逐时段跑（竞价/上午/午休/下午/收盘后），每段独立 hub，互不污染。
    还原成第一版过宽收敛时，probe_end 会被压到 09-15 使 delta_start > probe_end、
    整段探测被跳过 → 末端停在 09-15，本用例变红。
    """
    sessions = [
        (NOW_AUCTION, "开盘集合竞价"),
        (NOW_MORNING, "上午盘"),
        (NOW_LUNCH, "午休"),
        (NOW_AFTERNOON, "下午盘"),
        (NOW_POSTCLOSE, "收盘后"),
    ]
    for now, label in sessions:
        with tempfile.TemporaryDirectory() as tmp, _frozen_now(now):
            end = now.date()                              # 2026-09-16
            prov = ProbeCountingProvider(data_until=date(2026, 9, 15))
            hub = _make_hub(tmp, prov, cooldown=0)        # 隔离层 2，只验层 1
            start = end - timedelta(days=30)

            # 造一份只到上一交易日的缓存
            df0 = hub.get_bars([S1], Freq.D1, start, end, validate=False)
            _assert_delta_branch_reachable(df0, end, f"{label}/造缓存")
            assert pd.to_datetime(df0["date"]).max().date() == date(2026, 9, 15)
            n0 = len(prov.calls)

            # 数据源提供当日「正在形成」的日线 bar（实测 qmt/akshare 盘中均返回）
            prov.data_until = end
            df = hub.get_bars([S1], Freq.D1, start, end, validate=False)
            assert len(prov.calls) == n0 + 1, (
                f"{label}：应照常发起当日缺口探测，实际调用 {prov.calls[n0:]}")
            assert prov.calls[-1] == (end.isoformat(), end.isoformat()), (
                f"{label}：探测区间应为 [{end}, {end}]，实际 {prov.calls[-1]}")
            got_end = pd.to_datetime(df["date"]).max().date()
            assert got_end == end, (
                f"{label}：当日 bar 已发布却被收敛挡掉，末端停在 {got_end}（应 {end}）")
            assert len(df) == len(df0) + 1, f"{label}：应恰好多出当日一行"


# ========== 2c. case-B 守护：盘中「当日 bar」被范围缓存冻结后按 TTL 刷新覆盖 ==========
# 日线范围缓存 key 不含 end，盘中 cached_end==end → 增量分支 cached_end<end 恒假 →
# 当日那根 bar 冻结在首次取数时刻（实测 14:39 仍显示 09:40 的 close/vol）。case-B 用
# _today_refresh_due 按 TTL 主动重探 [今天,今天]、keep="last" 覆盖、指纹变化才写盘。
# ★ TTL 判定用真实墙钟 time.time()（非冻结的 datetime），故模拟到期须回拨
#   _today_refresh_at[k] -= (ttl+1)，用 _frozen_now 拨不动它。
def _close_on(df: pd.DataFrame, day: date) -> float:
    """取 df 中 day 那根 bar 的 close（断言恰有一根，避免切片歧义）。"""
    r = df[pd.to_datetime(df["date"]).dt.date == day]
    assert len(r) == 1, f"应恰有一根 {day} 的 bar，实际 {len(r)}"
    return float(r["close"].iloc[0])


def test_today_bar_refreshes_when_stale():
    """case-B 核心：盘中当日 bar 被范围缓存冻结后，过 TTL 必须重探并覆盖成新值。

    撤销 _today_refresh_due 分支后本用例变红：当日 close 会停在 100、无第二次探测。
    同时验证 t1（fresh-download 抑制）：首次下载已含当日 bar，紧接着**不应**再冗余
    重探 [今天,今天]（无 t1 时首调 prov.calls 会是 2 而非 1）。
    """
    with tempfile.TemporaryDirectory() as tmp, _frozen_now(NOW_MORNING):
        today = NOW_MORNING.date()                       # 2026-09-16
        start = today - timedelta(days=30)
        prov = IntradayTodayProvider(today, today_close=100.0, today_vol=1_000.0)
        hub = _make_hub(tmp, prov, cooldown=0, today_ttl=60)

        # 首调：磁盘 miss → 全量下载 [start, today]，拿到当日 bar(close=100)。
        # t1 记 _today_refresh_at[key] → 紧接着的增量分支在 TTL 内**不**冗余重探。
        df1 = hub.get_bars([S1], Freq.D1, start, today, validate=False)
        assert len(prov.calls) == 1, (
            f"首次下载已含当日 bar，t1 应抑制冗余重探，实际调用 {prov.calls}")
        assert prov.calls == [(start.isoformat(), today.isoformat())], prov.calls
        assert len(hub._today_refresh_at) == 1, "t1 应记录一次刷新时刻"
        assert _close_on(df1, today) == 100.0, f"首调当日 close 应 100，实际 {_close_on(df1, today)}"

        # 盘中价格跳动 + TTL 到期（回拨 _today_refresh_at，因 TTL 用真实墙钟）
        prov.today_close = 105.0
        prov.today_vol = 8_000.0
        for k in list(hub._today_refresh_at):
            hub._today_refresh_at[k] -= (60 + 1)

        df2 = hub.get_bars([S1], Freq.D1, start, today, validate=False)
        assert len(prov.calls) == 2, f"TTL 到期后应重探一次，实际调用 {prov.calls}"
        assert prov.calls[-1] == (today.isoformat(), today.isoformat()), (
            f"重探区间应为 [今天,今天]，实际 {prov.calls[-1]}")
        assert _close_on(df2, today) == 105.0, (
            f"当日 bar 应被刷新覆盖为 105，实际 {_close_on(df2, today)}（撤销 case-B 会停在 100）")


def test_today_bar_refresh_throttled_by_ttl():
    """TTL 节流：冷却窗内多次取数只探一次，到期后才再探（防每分钟重写盘争锁）。

    撤销 case-B 后「到期再探」不再发生 → 本用例变红。
    """
    with tempfile.TemporaryDirectory() as tmp, _frozen_now(NOW_AFTERNOON):
        today = NOW_AFTERNOON.date()
        start = today - timedelta(days=30)
        prov = IntradayTodayProvider(today, today_close=100.0)
        hub = _make_hub(tmp, prov, cooldown=0, today_ttl=60)

        hub.get_bars([S1], Freq.D1, start, today, validate=False)   # 首次下载
        base = len(prov.calls)
        assert base == 1, f"首调应只下载一次，实际 {prov.calls}"

        # TTL 窗内连点 5 次（模拟用户反复看图）→ 零重探
        for _ in range(5):
            hub.get_bars([S1], Freq.D1, start, today, validate=False)
        assert len(prov.calls) == base, (
            f"TTL 窗内不应重探；调用从 {base} 增至 {len(prov.calls)}: {prov.calls}")

        # TTL 到期 → 恰好再探一次 [今天,今天]
        for k in list(hub._today_refresh_at):
            hub._today_refresh_at[k] -= (60 + 1)
        hub.get_bars([S1], Freq.D1, start, today, validate=False)
        assert len(prov.calls) == base + 1, (
            f"TTL 到期后应重探一次，实际调用 {prov.calls}")
        assert prov.calls[-1] == (today.isoformat(), today.isoformat()), prov.calls[-1]


def test_today_bar_refresh_writes_through_to_disk():
    """刷新后的当日 bar 必须写穿到磁盘缓存（否则重启后又读回冻结的旧值）。

    指纹变化才落盘（_tail_fingerprint 取 close/volume 之和）；撤销 case-B 后当日
    bar 不更新 → 磁盘仍是旧值 → 本用例变红。
    """
    with tempfile.TemporaryDirectory() as tmp, _frozen_now(NOW_MORNING):
        today = NOW_MORNING.date()
        start = today - timedelta(days=30)
        adjust = Adjust.HFQ
        prov = IntradayTodayProvider(today, today_close=100.0)
        hub = _make_hub(tmp, prov, cooldown=0, today_ttl=60)

        hub.get_bars([S1], Freq.D1, start, today, adjust, validate=False)
        prov.today_close = 108.0                         # 盘中跳动
        for k in list(hub._today_refresh_at):
            hub._today_refresh_at[k] -= (60 + 1)         # TTL 到期
        hub.get_bars([S1], Freq.D1, start, today, adjust, validate=False)

        # 直接读磁盘（绕过内存缓存），确认新值已落盘
        hits, _missing = hub._load_bars_disk([S1], Freq.D1, start, adjust)
        assert S1 in hits, "刷新后 S1 应在磁盘缓存中"
        assert _close_on(hits[S1], today) == 108.0, (
            f"磁盘当日 bar 应为刷新后的 108，实际 {_close_on(hits[S1], today)}")


def test_today_bar_no_refresh_for_historical_end():
    """回测 PIT 安全：end 落在过去时，case-B 当日刷新必须完全 no-op。

    历史区间一旦取数即定型，之后的"盘中跳动"绝不能回改历史 bar（否则回测前视）。
    本用例在**撤销 case-B 后仍须保持绿** —— 对历史 end，_today_refresh_due 第一道
    判据 end.date()!=today 就返回 False，与是否存在刷新分支无关。
    """
    with tempfile.TemporaryDirectory() as tmp, _frozen_now(NOW_MORNING):
        real_today = NOW_MORNING.date()                  # 2026-09-16
        hist_end = real_today - timedelta(days=1)        # 2026-09-15，历史日
        start = hist_end - timedelta(days=30)
        prov = IntradayTodayProvider(hist_end, today_close=100.0)
        hub = _make_hub(tmp, prov, cooldown=0, today_ttl=60)

        df1 = hub.get_bars([S1], Freq.D1, start, hist_end, validate=False)
        assert pd.to_datetime(df1["date"]).max().date() == hist_end
        n1 = len(prov.calls)
        assert not hub._today_refresh_at, (
            f"历史 end 不应触发当日刷新记账，实际 {hub._today_refresh_at}")

        # 即便数据源"当日 bar"变了、TTL 也拨到期，历史 end 也绝不重探、不回改历史
        prov.today_close = 999.0
        for k in list(hub._today_refresh_at):
            hub._today_refresh_at[k] -= (60 + 1)
        df = df1
        for _ in range(3):
            df = hub.get_bars([S1], Freq.D1, start, hist_end, validate=False)
        assert len(prov.calls) == n1, (
            f"历史 end 永不重探；调用从 {n1} 增至 {len(prov.calls)}: {prov.calls}")
        assert _close_on(df, hist_end) == 100.0, (
            f"历史 bar 必须保持首次取数值 100（不得被回改成 999），实际 {_close_on(df, hist_end)}")


# ============================================ 3. 负缓存：探测失败后冷却窗内不重试
def test_negative_cache_suppresses_repeat_probe():
    """收敛后仍需探测（历史缺口）但失败 → 冷却窗内不再重试同一区间。"""
    with tempfile.TemporaryDirectory() as tmp:
        prov = ProbeCountingProvider(data_until=date(2026, 1, 7))
        hub = _make_hub(tmp, prov, cooldown=60)

        hub.get_bars([S1], Freq.D1, "2026-01-05", "2026-01-07", validate=False)
        n0 = len(prov.calls)

        # 第 1 次：缺口 [01-08, 01-09] → 探测 → 空 → DataUnavailableError → 记入负缓存
        hub.get_bars([S1], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        n1 = len(prov.calls)
        assert n1 == n0 + 1, f"首次缺口应发起一次探测，实际调用 {prov.calls}"
        assert hub._delta_probe_fail_at, "探测失败后应写入负缓存"

        # 第 2、3 次：冷却窗内 → 不再探测
        for _ in range(2):
            hub.get_bars([S1], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        assert len(prov.calls) == n1, \
            f"冷却窗内不应重复探测；调用从 {n1} 增至 {len(prov.calls)}: {prov.calls}"

        # 换个标的：负缓存 key 不含 symbol → 同样免探测
        # （只检查本阶段新增的调用；S2 首次仍需一次全量下载，那不是增量探测）
        hub.get_bars([S2], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        hub.cache.clear()                                  # 逼 S2 走磁盘/下载路径
        hub.get_bars([S2], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        s2_calls = prov.calls[n1:]
        assert not any(c[0] == "2026-01-08" for c in s2_calls), \
            f"负缓存应对全部标的生效，不应再探测 01-08 起的缺口: {s2_calls}"


def test_cooldown_zero_disables_negative_cache():
    """delta_probe_cooldown=0 → 关闭负缓存，每次请求都重新探测（可回退）。"""
    with tempfile.TemporaryDirectory() as tmp:
        prov = ProbeCountingProvider(data_until=date(2026, 1, 7))
        hub = _make_hub(tmp, prov, cooldown=0)
        assert hub.delta_probe_cooldown == 0

        hub.get_bars([S1], Freq.D1, "2026-01-05", "2026-01-07", validate=False)
        n0 = len(prov.calls)
        for _ in range(3):
            hub.get_bars([S1], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        assert len(prov.calls) == n0 + 3, \
            f"cooldown=0 时每次都应探测，实际调用 {prov.calls}"


def test_probe_success_clears_negative_entry():
    """探测成功必须清掉残留失败标记，否则后续真缺口会被错误压制。"""
    with tempfile.TemporaryDirectory() as tmp:
        prov = ProbeCountingProvider(data_until=date(2026, 1, 7))
        hub = _make_hub(tmp, prov, cooldown=60)
        hub.get_bars([S1], Freq.D1, "2026-01-05", "2026-01-07", validate=False)

        hub.get_bars([S1], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        assert hub._delta_probe_fail_at, "先制造一次失败"

        prov.data_until = None                             # 数据源恢复
        hub._delta_probe_fail_at.clear()                   # 模拟冷却窗已过
        df = hub.get_bars([S1], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        assert pd.to_datetime(df["date"]).max().date() == date(2026, 1, 9), \
            "缺口补齐后应覆盖到 01-09"
        assert not hub._delta_probe_fail_at, "探测成功后负缓存标记必须被清除"


# ------------------------------------------------------------------ 直接运行
if __name__ == "__main__":
    import logging
    logging.disable(logging.WARNING)          # 屏蔽 GBK 控制台下的乱码 warning
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}\n      {exc}")
        except Exception as exc:                          # noqa: BLE001
            failed += 1
            print(f"ERROR {name}\n      {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
