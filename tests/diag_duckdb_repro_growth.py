"""复现：重复 write_partitioned（模拟连续点击自动选股）是否撑大 DuckDB 文件。

背景
----
昨天做了 bars_cache 按标的分片(b1) + key 指纹(b2) 优化后，主库体积明显下降，
但用户反馈「多点几次自动选股，数据空间还是膨胀很快」。本脚本在**隔离的临时库**
上（绝不触碰被后端独占锁定的生产 qmt.duckdb）用真实的 MarketRepository 复现：

选股主链路 = pipeline.run → engine.build_panel(全 universe) → hub.get_bars(end=今天)
→ 命中增量路径时 _save_bars_disk(全量) → store.write_partitioned(...)。
write_partitioned 内部对每个标的 key 先 `DELETE ... WHERE __dataset_key IN (...)`
再 `INSERT BY NAME`。DuckDB 的 DELETE 只标记删除、不归还磁盘空间，全代码库又无
CHECKPOINT/VACUUM 收缩，故怀疑：**逻辑行数不变，物理文件却随每次重写单调增长**。

本脚本验证三件事：
  1) 相同 key 重复 write_partitioned K 次 → 主库文件是否单调增长（每次点击的增量）；
  2) 行数是否恒定（证明是空间泄漏，不是数据堆积）；
  3) evict_expired 全删 + CHECKPOINT 后文件是否收缩（证明 DELETE 不回收空间）。

用法：
    D:/programs/Python311/python.exe tests/diag_duckdb_repro_growth.py [symbols] [days] [iters]
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qmt_trade.storage.duckdb.database import Database  # noqa: E402
from qmt_trade.storage.market import MarketRepository  # noqa: E402

MB = 1024 * 1024


def build_frame(n_symbols: int, n_days: int) -> pd.DataFrame:
    """构造与 bars_cache 同构的合成日线面板（symbol/date/ohlcv/prev_close）。"""
    rng = np.random.default_rng(42)
    dates = pd.bdate_range(end="2026-09-15", periods=n_days)
    syms = [f"{i:06d}.SZ" for i in range(1, n_symbols + 1)]
    n = n_symbols * n_days
    close = np.round(rng.lognormal(mean=2.3, sigma=0.5, size=n), 2)
    df = pd.DataFrame({
        "symbol": np.repeat(syms, n_days),
        "date": np.tile(dates, n_symbols),
        "open": np.round(close * rng.uniform(0.98, 1.02, n), 2),
        "high": np.round(close * rng.uniform(1.00, 1.05, n), 2),
        "low": np.round(close * rng.uniform(0.95, 1.00, n), 2),
        "close": close,
        "volume": rng.integers(100_000, 10_000_000, n),
        "amount": np.round(close * rng.integers(100_000, 10_000_000, n), 2),
        "prev_close": np.round(close * rng.uniform(0.95, 1.05, n), 2),
    })
    return df


def file_sizes(dbpath: Path) -> tuple[int, int]:
    wal = Path(str(dbpath) + ".wal")
    return dbpath.stat().st_size, (wal.stat().st_size if wal.exists() else 0)


def main() -> int:
    n_symbols = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
    n_days = int(sys.argv[2]) if len(sys.argv) > 2 else 250
    iters = int(sys.argv[3]) if len(sys.argv) > 3 else 6

    df = build_frame(n_symbols, n_days)
    rows = len(df)
    print(f"合成面板：{n_symbols} 标的 × {n_days} 交易日 = {rows:,} 行，{len(df.columns)} 列")
    print(f"（真实全市场约 5,219 只 × ~270 日 ≈ 140 万行，可按每行字节数外推）\n")

    # 每标的的磁盘 key，与 manager._bars_disk_sym_key 同构（短 key，已含指纹前的明文）
    def key_fn(s):
        return repr((s, "1d", "2025-11-01", "hfq", ("akshare", "qmt"), 3))

    tmpdir = Path(tempfile.mkdtemp(prefix="duckdb_repro_"))
    dbpath = tmpdir / "repro.duckdb"
    db = Database(str(dbpath), schema="market")
    repo = MarketRepository(db)
    table = repo._table("bars_cache")

    print(f"临时库：{dbpath}\n")
    print(f"{'迭代':>4} {'写入key数':>9} {'行数':>12} {'主库MB':>10} {'WAL MB':>9} "
          f"{'主库ΔMB':>10} {'耗时s':>7}")

    prev_db = 0
    growth = []
    for it in range(1, iters + 1):
        t0 = time.time()
        n_keys = repo.write_partitioned("bars_cache", df, key_fn=key_fn, partition_col="symbol")
        # 生产依赖 DuckDB 自动 checkpoint（WAL>16MB）；这里显式 checkpoint 让主库体积
        # 反映已提交数据，测量更干净。即便 checkpoint，DELETE 释放的空间也不会收缩文件。
        db.execute("CHECKPOINT")
        db_size, wal_size = file_sizes(dbpath)
        cnt = db.scalar(f"SELECT count(*) FROM {table}")
        d = (db_size - prev_db) / MB
        if it > 1:
            growth.append(d)
        print(f"{it:>4} {n_keys:>9} {cnt:>12,} {db_size/MB:>10.3f} {wal_size/MB:>9.3f} "
              f"{d:>+10.3f} {time.time()-t0:>7.2f}")
        prev_db = db_size

    print("\n=== 结论 1/2：相同 key 重复重写 ===")
    if growth:
        avg = sum(growth) / len(growth)
        per_row = avg * MB / rows if rows else 0
        print(f"  首次写入后，每次重写主库净增 ≈ {avg:+.3f} MB（行数恒定 {rows:,}）")
        print(f"  每行物理占用 ≈ {per_row:.1f} B/行")
        print(f"  外推全市场 140 万行：每次点击 ≈ {per_row*1_400_000/MB:.1f} MB")
        print(f"  → 逻辑数据没变，文件却每次点击单调增长 = 空间泄漏（DELETE 不回收）")

    print("\n=== 结论 3：evict_expired 全删 + CHECKPOINT 能否收缩 ===")
    before_db, _ = file_sizes(dbpath)
    removed = repo.evict_expired("bars_cache", ttl_seconds=0)  # ttl=0 → 全部过期
    db.execute("CHECKPOINT")
    after_db, _ = file_sizes(dbpath)
    cnt = db.scalar(f"SELECT count(*) FROM {table}")
    print(f"  淘汰 key 数 = {removed}，剩余行数 = {cnt}")
    print(f"  删除前主库 = {before_db/MB:.3f} MB，删除后 = {after_db/MB:.3f} MB "
          f"（Δ{(after_db-before_db)/MB:+.3f} MB）")
    if after_db >= before_db * 0.95:
        print(f"  → 行已全删，文件几乎不收缩：DuckDB DELETE 不归还磁盘空间，需 VACUUM/重建")

    db.close()
    print(f"\n临时库目录：{tmpdir}（可安全删除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
