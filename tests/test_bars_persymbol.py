"""bars_cache 按标的分片磁盘缓存（b1）回归测试。

b1 根因：旧磁盘 key 把整个股票池塞进单一 key（全市场硬筛一次 5,219 只 →
67,912 字符），池组成每变一次 key 就完全不同、永不命中，每次扫描/回测都从
数据源全量重拉全市场日线——系统卡顿根因。修复=按标的分片：每 symbol 独立
定长短 key，池变化时重叠标的仍命中，只下载真正新增的标的。

本测试用内存 MarketRepository 作 store + 合成数据源（name 不含 "mock"，故
磁盘缓存启用）直接驱动 DataHub.get_bars，**不触碰生产 qmt.duckdb**，覆盖：
  1. 存储层 write_partitioned / read_many_batch 往返一致 + TTL 过期 + 幂等重写
  2. 跨池复用：重叠标的命中磁盘，只对新增标的下载（b1 核心断言）
  3. per-symbol key 定长极短、coverage 行数 == 标的数（非 1 条池级 key）
  4. TTL 过期后强制重拉
  5. 增量 gap-fill 仍工作（磁盘命中基线 + 只补缺口）
  6. fail-safe：无命中且下载失败 → 抛；有部分命中 → 降级沿用命中标的

运行：pytest tests/test_bars_persymbol.py  或  python tests/test_bars_persymbol.py
（遵守项目规则：所有测试用 Python 脚本，禁用 curl。）
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qmt_trade.core.config import Settings  # noqa: E402
from qmt_trade.core.errors import DataUnavailableError  # noqa: E402
from qmt_trade.core.instruments import normalize_symbol  # noqa: E402
from qmt_trade.datahub.manager import DataHub  # noqa: E402
from qmt_trade.datahub.providers.base import Capability, DataProvider  # noqa: E402
from qmt_trade.datahub.types import Adjust, Freq  # noqa: E402
from qmt_trade.storage.db import Database  # noqa: E402
from qmt_trade.storage.market import MarketRepository  # noqa: E402

S1, S2, S3, S4 = "600000.SH", "000001.SZ", "600519.SH", "300750.SZ"


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
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    vol = rng.integers(1_000_00, 2_000_000, n).astype(float)
    return pd.DataFrame({
        "date": dates,
        "symbol": symbol,
        "open": open_.round(2), "high": high.round(2),
        "low": low.round(2), "close": close.round(2),
        "volume": vol,
    })


class SyntheticProvider(DataProvider):
    """name 不含 'mock' → DataHub 磁盘缓存对其启用。记录每次被请求的标的集合。"""

    name = "synthetic"
    capabilities = {Capability.BARS}

    def __init__(self, **kw):
        super().__init__(**kw)
        self.fail = False
        self.total_calls = 0
        self.last_asked: set[str] = set()

    def is_available(self) -> bool:
        return True

    def get_bars(self, symbols, freq=Freq.D1, start=None, end=None, adjust=Adjust.HFQ):
        self.total_calls += 1
        self.last_asked = {normalize_symbol(s) for s in symbols}
        if self.fail:
            raise RuntimeError("synthetic down")
        frames = [_synth_bars(normalize_symbol(s), start, end) for s in symbols]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)


def _make_hub(tmp: str, provider: DataProvider) -> DataHub:
    """构造隔离 DataHub：data_dir 指向临时目录，store 用内存库（不碰生产 qmt.duckdb）。"""
    st = Settings({
        "app": {"data_dir": tmp},
        "datahub": {
            "priority": {"bars": ["synthetic"]},
            "circuit_breaker": {"fail_threshold": 10, "cooldown_seconds": 300},
            "cache": {"max_items": 4096, "daily_bar_ttl": 86400},
            "quality": {"max_missing_ratio": 0.2, "max_abs_return": 0.35},
        },
    }, env_overlay=False)
    store = MarketRepository(Database(":memory:", schema="market"))
    return DataHub(st, [provider], store=store)


# ------------------------------------------------------------------ 1. 存储层往返
def test_partitioned_roundtrip():
    repo = MarketRepository(Database(":memory:", schema="market"))
    df = pd.concat([_synth_bars(s, "2026-01-05", "2026-01-09") for s in (S1, S2, S3)],
                   ignore_index=True)
    n = repo.write_partitioned("bars_cache", df, key_fn=lambda s: f"K::{s}", partition_col="symbol")
    assert n == 3, f"应写入 3 个 per-symbol key，实际 {n}"

    keys = [f"K::{s}" for s in (S1, S2, S3)]
    out = repo.read_many_batch("bars_cache", keys)
    assert set(out) == set(keys), "批量读回的 key 集合应与写入一致"
    for k in keys:
        f = out[k]
        assert "__dataset_key" not in f.columns, "内部指纹列不应泄漏给调用方"
        assert len(f) == 5, f"{k} 应有 5 行日线，实际 {len(f)}"
        assert (f["symbol"] == k.split("::")[1]).all(), f"{k} 分片混入了其他标的"
        assert f["date"].is_monotonic_increasing, f"{k} 未按日期升序"

    # 未写入的 key → miss
    assert repo.read_many_batch("bars_cache", ["K::999999.SH"]) == {}
    # TTL：把 now 推到写入时间之后远超 ttl → 全部过期
    assert repo.read_many_batch("bars_cache", keys, ttl_seconds=1, now=time.time() + 100) == {}

    # 幂等重写：同 key 覆盖，不产生重复行
    repo.write_partitioned("bars_cache", df, key_fn=lambda s: f"K::{s}", partition_col="symbol")
    table = repo._table("bars_cache")
    assert repo.db.scalar(f"SELECT count(*) FROM {table}") == 15, "重写后应仍是 3*5=15 行（无重复）"


# ------------------------------------------------------------------ 2. 跨池复用（b1 核心）
def test_cross_pool_reuse_only_downloads_missing():
    with tempfile.TemporaryDirectory() as tmp:
        prov = SyntheticProvider()
        hub = _make_hub(tmp, prov)

        # 池 A：3 个标的全部首次下载
        dfA = hub.get_bars([S1, S2, S3], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        assert prov.last_asked == {S1, S2, S3}, "池 A 首次应下载全部 3 个标的"
        assert set(dfA["symbol"]) == {S1, S2, S3}
        keysA = set(hub.store.list_keys("bars_cache"))
        assert len(keysA) == 3, f"应落 3 条 per-symbol coverage，实际 {len(keysA)}"

        # 清内存缓存，强制走磁盘路径（模拟跨进程/跨池重跑）
        hub.cache.clear()

        # 池 B：与 A 重叠 S2/S3，新增 S4 → 只应下载 S4
        dfB = hub.get_bars([S2, S3, S4], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        assert prov.last_asked == {S4}, (
            f"b1 核心：重叠标的应命中磁盘，只下载新增 S4，实际下载 {prov.last_asked}")
        assert set(dfB["symbol"]) == {S2, S3, S4}, "池 B 结果应含全部 3 个标的（2 命中 + 1 新下载）"
        # 命中标的数据须与池 A 一致（磁盘读回未损坏）
        for s in (S2, S3):
            a = dfA[dfA["symbol"] == s][["date", "close"]].reset_index(drop=True)
            b = dfB[dfB["symbol"] == s][["date", "close"]].reset_index(drop=True)
            pd.testing.assert_frame_equal(a, b)

        # coverage 增至 4 条，且每条 key 定长极短（不再是 67KB 池级明文 key）
        keysB = set(hub.store.list_keys("bars_cache"))
        assert len(keysB) == 4, f"应落 4 条 coverage，实际 {len(keysB)}"
        assert all(len(k) < 200 for k in keysB), "per-symbol key 必须定长极短"


def test_persymbol_key_is_short_and_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        prov = SyntheticProvider()
        hub = _make_hub(tmp, prov)
        key = hub._bars_disk_sym_key(S1, Freq.D1, "2026-01-05", Adjust.HFQ)
        assert len(repr(key)) < 200, "单个标的 key 应定长极短"
        # key 与池大小无关：无论请求多少标的，每标的 key 长度恒定
        big_pool = [f"{600000 + i}.SH" for i in range(5000)]
        k0 = repr(hub._bars_disk_sym_key(big_pool[0], Freq.D1, "2026-01-05", Adjust.HFQ))
        k1 = repr(hub._bars_disk_sym_key(big_pool[-1], Freq.D1, "2026-01-05", Adjust.HFQ))
        assert len(k0) == len(k1) < 200, "per-symbol key 长度不随池规模增长"


# ------------------------------------------------------------------ 3. TTL 过期强制重拉
def test_ttl_expiry_forces_redownload():
    with tempfile.TemporaryDirectory() as tmp:
        prov = SyntheticProvider()
        hub = _make_hub(tmp, prov)
        hub.get_bars([S1, S2], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        assert prov.total_calls == 1

        # 把 coverage 写入时间推到 TTL 之外 → 磁盘缓存全部过期
        hub.store.db.update(
            "dataset_coverage",
            {"written_at": time.time() - hub._BARS_DISK_TTL - 100},
            "dataset=?", ["bars_cache"])
        hub.cache.clear()

        hub.get_bars([S1, S2], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        assert prov.last_asked == {S1, S2}, "TTL 过期后应重新下载全部标的"
        assert prov.total_calls == 2, "过期未命中 → 触发一次新的下载"


# ------------------------------------------------------------------ 4. 增量 gap-fill
def test_incremental_gapfill_still_works():
    with tempfile.TemporaryDirectory() as tmp:
        prov = SyntheticProvider()
        hub = _make_hub(tmp, prov)
        # 基线：01-05 ~ 01-09
        hub.get_bars([S1, S2], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        assert prov.total_calls == 1
        hub.cache.clear()

        # 请求延长到 01-14：磁盘 key 不含 end → 基线命中，仅补缺口 01-10~01-14
        df = hub.get_bars([S1, S2], Freq.D1, "2026-01-05", "2026-01-14", validate=False)
        assert prov.total_calls == 2, "基线应命中磁盘，仅增量调用一次数据源"

        for s in (S1, S2):
            sub = df[df["symbol"] == s]
            assert sub["date"].max() == pd.Timestamp("2026-01-14"), f"{s} 增量未补齐到 01-14"
            assert not sub.duplicated(["date"]).any(), f"{s} 增量合并出现重复日期"
            # 01-05..09(5) + 01-12..14(3) = 8 个工作日
            assert len(sub) == 8, f"{s} 应有 8 行，实际 {len(sub)}"


# ------------------------------------------------------------------ 5. fail-safe
def test_failsafe_all_missing_raises():
    with tempfile.TemporaryDirectory() as tmp:
        prov = SyntheticProvider()
        prov.fail = True
        hub = _make_hub(tmp, prov)
        try:
            hub.get_bars([S1, S2], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
            raised = False
        except DataUnavailableError:
            raised = True
        assert raised, "无任何磁盘命中且下载失败 → 必须抛 DataUnavailableError（fail-safe）"


def test_failsafe_partial_hit_degrades():
    with tempfile.TemporaryDirectory() as tmp:
        prov = SyntheticProvider()
        hub = _make_hub(tmp, prov)
        # 先把 S1 落盘
        hub.get_bars([S1], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        hub.cache.clear()
        # 数据源宕机：请求 [S1, S2] → S1 命中磁盘，S2 下载失败 → 降级沿用 S1，不抛
        prov.fail = True
        df = hub.get_bars([S1, S2], Freq.D1, "2026-01-05", "2026-01-09", validate=False)
        assert set(df["symbol"]) == {S1}, "部分命中时应降级返回已命中标的，不拖垮全池"


if __name__ == "__main__":
    fns = [
        test_partitioned_roundtrip,
        test_cross_pool_reuse_only_downloads_missing,
        test_persymbol_key_is_short_and_bounded,
        test_ttl_expiry_forces_redownload,
        test_incremental_gapfill_still_works,
        test_failsafe_all_missing_raises,
        test_failsafe_partial_hit_degrades,
    ]
    for fn in fns:
        fn()
        print(f"  [OK] {fn.__name__}")
    print("PERSYMBOL_TEST_OK")
