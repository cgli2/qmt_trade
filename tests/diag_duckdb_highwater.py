"""决定性实验 2：复现生产「双代 bars 共存 → 淘汰旧代 → 文件不收缩」的高水位机制。

背景（前两个实验已确立的事实）
------------------------------
- diag_duckdb_repro_growth.py：**相同 key** 重复 write_partitioned → 文件振荡有界；
  evict 全删 + 显式 CHECKPOINT → 文件收缩。
- diag_duckdb_checkpoint_effect.py：相同 key 重复写，有无显式 CHECKPOINT 结果一致
  （A==B，都振荡有界，WAL 恒 0，自动 checkpoint 会压缩大写事务）。
  → 证伪「无 CHECKPOINT 就会单调棘轮」的假设。

尚未被任何实验覆盖的、生产真实发生的序列
----------------------------------------
生产 bars 磁盘 key 含 str(start)，start=asof-窗口天数。asof=today-1，故 **每日漂移**：
今天的选股写「新一代」key，昨天的「旧一代」key 因 TTL=12h 尚未淘汰 → **两代共存**，
文件涨到峰值；12h 后 evict_expired 把旧代 DELETE 掉，但 DELETE **不做 CHECKPOINT**，
释放的块只在文件内部标记为 free、**不归还给文件**，于是文件**卡在高水位**。

本脚本用隔离临时库精确复现该序列并逐步测体积：
  1) 写 gen1（start=S1）
  2) 把 gen1 的 coverage.written_at 回拨到过期
  3) 写 gen2（start=S2，不同 key）→ 两代共存 = 峰值
  4) evict_expired(ttl) 只删 gen1（不 CHECKPOINT）→ 看文件是否**停在峰值**
  5) 显式 CHECKPOINT → 看文件是否**收缩回单代**

判据：
  - 若 (4) 后文件≈峰值(不降)、(5) 后文件≈单代 → 坐实「高水位卡死」根因：
    每日双代共存抬升峰值 + 淘汰不 CHECKPOINT → 文件只涨不落，锁死在峰值。
  - 若 (4) 后文件已自动回落 → 高水位假设不成立，需另寻增长源。

用法：
    D:/programs/Python311/python.exe tests/diag_duckdb_highwater.py [symbols] [days]
"""
from __future__ import annotations

import shutil
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


def build_frame(n_symbols: int, n_days: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end="2026-09-15", periods=n_days)
    syms = [f"{i:06d}.SZ" for i in range(1, n_symbols + 1)]
    n = n_symbols * n_days
    close = np.round(rng.lognormal(mean=2.3, sigma=0.5, size=n), 2)
    return pd.DataFrame({
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


def sizes(dbpath: Path) -> tuple[float, float]:
    wal = Path(str(dbpath) + ".wal")
    return (dbpath.stat().st_size / MB,
            (wal.stat().st_size / MB if wal.exists() else 0.0))


def main() -> int:
    n_symbols = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    n_days = int(sys.argv[2]) if len(sys.argv) > 2 else 250

    gen1 = build_frame(n_symbols, n_days, seed=7)
    gen2 = build_frame(n_symbols, n_days, seed=99)  # 不同数据，模拟新一天的行情
    rows = len(gen1)

    tmp = Path(tempfile.mkdtemp(prefix="highwater_"))
    dbpath = tmp / "t.duckdb"
    db = Database(str(dbpath), schema="market")
    repo = MarketRepository(db)
    table = repo._table("bars_cache")

    def key_s1(s):
        return repr((s, "1d", "2025-11-01", "hfq", ("akshare", "qmt"), 3))

    def key_s2(s):
        return repr((s, "1d", "2025-11-02", "hfq", ("akshare", "qmt"), 3))

    def snap(label: str):
        d, w = sizes(dbpath)
        try:
            cnt = db.scalar(f"SELECT count(*) FROM {table}")
        except Exception:  # noqa: BLE001 - 表在首次 write_partitioned 前尚未创建
            cnt = 0
        try:
            cov = db.scalar("SELECT count(*) FROM dataset_coverage WHERE dataset='bars_cache'")
        except Exception:  # noqa: BLE001
            cov = 0
        print(f"  {label:<34} 主库={d:8.3f}MB  WAL={w:6.3f}MB  合计={d+w:8.3f}MB  "
              f"行数={cnt:>10,}  coverage_key={cov:>7,}")
        return d  # 判定只看主库文件（=用户关心的“数据空间”）；WAL 是瞬态、CHECKPOINT 会刷入主库

    print(f"合成面板：每代 {n_symbols} × {n_days} = {rows:,} 行；两代不同 start → 不同 key\n")
    base = snap("步骤0 空库基线")

    print("\n=== 步骤1：写 gen1（start=2025-11-01）===")
    repo.write_partitioned("bars_cache", gen1, key_fn=key_s1, partition_col="symbol")
    after1 = snap("步骤1 单代 gen1")

    print("\n=== 步骤2：把 gen1 的 coverage.written_at 回拨到过期 ===")
    old_ts = time.time() - 100000
    db.execute("UPDATE dataset_coverage SET written_at=? WHERE dataset='bars_cache'", [old_ts])
    snap("步骤2 回拨后（体积不变）")

    print("\n=== 步骤3：写 gen2（start=2025-11-02，不同 key）→ 两代共存 = 峰值 ===")
    repo.write_partitioned("bars_cache", gen2, key_fn=key_s2, partition_col="symbol")
    peak = snap("步骤3 双代共存(峰值)")

    print("\n=== 步骤4：evict_expired(ttl=3600) 只删 gen1（不 CHECKPOINT）===")
    removed = repo.evict_expired("bars_cache", 3600, now=time.time())
    after_evict = snap(f"步骤4 淘汰旧代(删了{removed}个key)")

    print("\n=== 步骤5：显式 CHECKPOINT → 释放的块归还文件 ===")
    db.execute("CHECKPOINT")
    after_ckpt = snap("步骤5 CHECKPOINT 后")

    single_gen = after1 - base
    print("\n" + "=" * 74)
    print(f"单代逻辑体积(gen1 主库)   : {single_gen:8.3f} MB")
    print(f"双代共存峰值(主库)        : {peak:8.3f} MB  (较单代 +{peak - after1:.3f})")
    print(f"淘汰旧代后主库(不CKPT)    : {after_evict:8.3f} MB  "
          f"(较峰值 {after_evict - peak:+.3f} —— 期望≈0，即 DELETE 不收缩主库)")
    print(f"CHECKPOINT 后主库         : {after_ckpt:8.3f} MB  "
          f"(较淘汰后 {after_ckpt - after_evict:+.3f}；较单代 {after_ckpt - after1:+.3f})")

    stuck = after_evict > peak - single_gen * 0.3                 # 淘汰后主库仍卡在峰值
    ckpt_reclaims = after_ckpt < after_evict - single_gen * 0.5   # CKPT 把主库压回接近单代
    print("\n判定：")
    if not stuck:
        print("  [NO]    淘汰后主库已回落 → 高水位假设不成立，需另寻增长源。")
    elif ckpt_reclaims:
        print("  [OK]    坐实【高水位卡死】：DELETE 不收缩主库，但显式 CHECKPOINT 能压回单代。")
        print("          生产零 CHECKPOINT/VACUUM → 文件锁死在双代峰值；定期 CHECKPOINT 即可回收。")
    else:
        print("  [WORSE] 坐实【高水位卡死·CHECKPOINT 也无法回收】：")
        print("          DELETE 不收缩主库(停在双代峰值)，且显式 CHECKPOINT 仅刷 WAL/回收尾部，")
        print("          主库仍远高于单代 → 中间空闲块无法归还，文件永久锁死在双代高水位。")
        print("          需 VACUUM/全量重写(导出再导入)才能真正收缩；峰值随 universe/窗口抬高。")

    db.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
