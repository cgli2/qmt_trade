"""bars_cache 过期淘汰（b2）回归测试。

验证 MarketRepository.evict_expired：只删过期 key、保留新鲜 key、行级数据按指纹
同步清除、且幂等。用内存库直接驱动，不触碰生产 qmt.duckdb。

背景：TTL 此前只作用于读命中判断，过期行从不删除，随股票池变化无限堆积，
是 qmt.duckdb 膨胀到 314 GiB 的第二成因（第一成因是明文 key 逐行复制，已由
key_fingerprint 指纹修复）。
"""
import time

import pandas as pd

from qmt_trade.storage.db import Database
from qmt_trade.storage.market import MarketRepository, key_fingerprint


def _bars(symbol, n=3):
    return pd.DataFrame({
        "symbol": [symbol] * n,
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": [10.0] * n, "high": [11.0] * n,
        "low": [9.0] * n, "close": [10.5] * n,
        "volume": [1000] * n,
    })


def test_evict_expired():
    repo = MarketRepository(Database(":memory:", schema="market"))
    ttl = 100  # 秒

    old_keys = [("OLD_POOL_A", "1d", "2026-01-01", "hfq"),
                ("OLD_POOL_B", "1d", "2026-01-01", "hfq")]
    fresh_key = ("FRESH_POOL", "1d", "2026-01-01", "hfq")

    now = time.time()
    for i, k in enumerate(old_keys):
        repo.write("bars_cache", _bars(f"00000{i}.SZ"), repr(k))
        repo.db.update("dataset_coverage", {"written_at": now - ttl - 50},
                       "dataset=? AND key=?", ["bars_cache", repr(k)])
    repo.write("bars_cache", _bars("600000.SH"), repr(fresh_key))
    repo.db.update("dataset_coverage", {"written_at": now},
                   "dataset=? AND key=?", ["bars_cache", repr(fresh_key)])

    table = repo._table("bars_cache")
    assert repo.db.scalar(f"SELECT count(*) FROM {table}") == 9  # 3 key * 3 行
    assert set(repo.list_keys("bars_cache")) == {repr(k) for k in old_keys} | {repr(fresh_key)}

    # ---- 执行淘汰 ----
    removed = repo.evict_expired("bars_cache", ttl, now=now)
    assert removed == 2, f"应淘汰 2 个过期 key，实际 {removed}"
    assert set(repo.list_keys("bars_cache")) == {repr(fresh_key)}, "过期 key 未清干净"

    # 过期 key 的行级数据必须按指纹删除
    for k in old_keys:
        fp = key_fingerprint(repr(k))
        assert repo.db.scalar(f"SELECT count(*) FROM {table} WHERE __dataset_key=?", [fp]) == 0

    # 新鲜 key 完整保留
    assert len(repo.read("bars_cache", repr(fresh_key))) == 3
    assert repo.db.scalar(f"SELECT count(*) FROM {table}") == 3

    # 幂等：再淘汰一次不应报错、不应误删
    assert repo.evict_expired("bars_cache", ttl, now=now) == 0
    assert len(repo.read("bars_cache", repr(fresh_key))) == 3


if __name__ == "__main__":
    test_evict_expired()
    print("EVICT_TEST_OK")
