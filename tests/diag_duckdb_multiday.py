"""决定性实验 3：多日 gen 漂移 + 每日淘汰，判定文件是「有界高水位」还是「无界棘轮」。

前序实验已确立的事实
--------------------
- diag_duckdb_checkpoint_effect.py：**相同 key** 重复 write_partitioned → 有界振荡
  （当日内多次点击不会单调增长；自动 checkpoint 会压缩大写事务）。
- diag_duckdb_highwater.py：**双代共存 → 淘汰一代 → 即使显式 CHECKPOINT**，主库也只从
  23.762→23.262MB，**没回到单代 11.5MB** → 文件锁死在双代高水位（[WORSE]）。

尚未回答、且决定严重性与修复方向的关键问题
------------------------------------------
生产 bars 磁盘 key 含 str(start)，start=asof-窗口天数、**每日漂移**。跨多日：
每天写新一代、淘汰前一代之后，主库究竟是
  (A) 稳定在 ~2× 单代（**有界高水位**：淘汰释放的 free 块被次日新代复用），还是
  (B) 每日 +1× 单代单调上涨（**无界棘轮**：free 块从不复用，文件永远只涨不落）？

序列（忠实复现生产：今日新代与昨日旧代在 12h TTL 内共存，随后旧代被淘汰）：
  day1: 写 gen1
  day2: 写 gen2（gen1 仍在 TTL 内 → 双代共存峰值）→ 淘汰 gen1
  day3: 写 gen3（gen2 仍在 TTL 内 → 双代共存）→ 淘汰 gen2
  ...  每步 snap 主库体积；最后再试显式 CHECKPOINT / VACUUM 能否回收。

判据：
  - day3 之后「淘汰后主库」趋于平台（≈2× 单代）→ 有界高水位（浪费但不失控）。
  - 「淘汰后主库」每日近似线性 +1× 单代 → 无界棘轮（必须 VACUUM/重建才能回收）。

用法：
    D:/programs/Python311/python.exe tests/diag_duckdb_multiday.py [symbols] [days] [ngen]
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
    ngen = int(sys.argv[3]) if len(sys.argv) > 3 else 6

    tmp = Path(tempfile.mkdtemp(prefix="multiday_"))
    dbpath = tmp / "t.duckdb"
    db = Database(str(dbpath), schema="market")
    repo = MarketRepository(db)
    table = repo._table("bars_cache")

    def key_for(start: str):
        def kf(s):
            return repr((s, "1d", start, "hfq", ("akshare", "qmt"), 3))
        return kf

    def snap(label: str) -> float:
        d, w = sizes(dbpath)
        try:
            cnt = db.scalar(f"SELECT count(*) FROM {table}")
        except Exception:  # noqa: BLE001 - 表在首次 write_partitioned 前尚未创建
            cnt = 0
        try:
            cov = db.scalar("SELECT count(*) FROM dataset_coverage WHERE dataset='bars_cache'")
        except Exception:  # noqa: BLE001
            cov = 0
        print(f"  {label:<30} 主库={d:8.3f}MB  WAL={w:6.3f}MB  "
              f"行数={cnt:>10,}  coverage_key={cov:>7,}")
        return d  # 只看主库文件（=用户关心的“数据空间”）

    print(f"合成面板：每代 {n_symbols} × {n_days} = {n_symbols*n_days:,} 行；"
          f"共 {ngen} 代，每代不同 start → 不同 key\n")
    base = snap("day0 空库基线")

    peaks, floors = [], []
    single_gen = None
    for i in range(1, ngen + 1):
        start = f"2025-11-{i:02d}"
        gen = build_frame(n_symbols, n_days, seed=100 + i)
        print(f"\n=== day{i}：写 gen{i}（start={start}）===")
        repo.write_partitioned("bars_cache", gen, key_fn=key_for(start), partition_col="symbol")
        after_write = snap(f"day{i} 写后(与旧代共存)")
        # 只保留最新代：把非本代 key 的 written_at 回拨到过期，再 evict（不做 CHECKPOINT）
        old_ts = time.time() - 100000
        db.execute("UPDATE dataset_coverage SET written_at=? "
                   "WHERE dataset='bars_cache' AND key NOT LIKE ?", [old_ts, f"%{start}%"])
        removed = repo.evict_expired("bars_cache", 3600, now=time.time())
        after_evict = snap(f"day{i} 淘汰旧代(删{removed})后")
        peaks.append(after_write)
        floors.append(after_evict)
        if i == 1:
            single_gen = after_evict - base

    print("\n=== 收尾：显式 CHECKPOINT，再试 VACUUM，看能否回收 ===")
    db.execute("CHECKPOINT")
    after_ckpt = snap("CHECKPOINT 后")
    try:
        db.execute("VACUUM")
        after_vac = snap("VACUUM 后")
    except Exception as exc:  # noqa: BLE001 - VACUUM 行为随版本而异
        after_vac = after_ckpt
        print(f"  (VACUUM 不可用: {exc})")

    print("\n" + "=" * 74)
    print(f"单代逻辑体积            : {single_gen:8.3f} MB")
    print("每日「淘汰后主库」序列（=文件地板，逻辑上只剩 1 代）：")
    for i, f in enumerate(floors, 1):
        ratio = (f - base) / single_gen if single_gen else 0
        print(f"  day{i}: {f:8.3f} MB  ≈ {ratio:4.2f}× 单代")
    # 后半程每日地板增量：>0.5×单代/日 → 无界棘轮；≈0 → 有界高水位
    tail = floors[max(1, len(floors) // 2):]
    per_day = (tail[-1] - tail[0]) / max(len(tail) - 1, 1) if len(tail) > 1 else 0
    print(f"\n后半程地板日均增量      : {per_day:+.3f} MB/日 "
          f"(= {per_day/single_gen if single_gen else 0:+.2f}× 单代/日)")
    print(f"CHECKPOINT 后 / VACUUM 后: {after_ckpt:.3f} / {after_vac:.3f} MB "
          f"(单代={single_gen+base:.3f})")

    print("\n判定：")
    unbounded = per_day > single_gen * 0.5
    if unbounded:
        print("  [RATCHET] 无界棘轮坐实：每日 gen 漂移 + 淘汰不回收 → 主库地板每日 +≈1× 单代")
        print("            单调上涨，free 块从不被次日新代复用。文件会无限增长，")
        print("            唯一可靠回收 = 停后端后全量重建(rebuild_runtime_db 导出再导入)。")
    else:
        print("  [PLATEAU] 有界高水位：淘汰后主库趋于平台(≈%.1f× 单代)，free 块被次日新代复用，"
              % ((floors[-1]-base)/single_gen if single_gen else 0))
        print("            文件不会无限增长，但**永久停在 ~2× 单代的高水位**(逻辑仅 1 代)，")
        print("            且每次淘汰/CHECKPOINT 都不回收中间空闲块 → 浪费≈1× 单代空间。")
    if after_vac < floors[-1] - single_gen * 0.3:
        print("  → VACUUM 能明显回收：可作为定期维护手段。")
    else:
        print("  → VACUUM/CHECKPOINT 均无法回收高水位：只能靠全量重建。")

    db.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
