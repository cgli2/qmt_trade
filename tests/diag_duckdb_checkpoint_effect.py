"""决定性实验：隔离「显式 CHECKPOINT」这一个变量，验证 DuckDB 空间回收行为。

背景
----
diag_duckdb_repro_growth.py 已证明：重复 write_partitioned（相同 key）后**显式**
`CHECKPOINT`，文件在 9~17MB 之间振荡、全删后能收缩——即 DELETE 的空间在
checkpoint 时会被回收。但生产 qmt.duckdb 全代码库**零** CHECKPOINT/VACUUM
（已 grep 确认），只靠 DuckDB 自动 checkpoint（WAL>16MB 时把 WAL 刷进主库）。
线上证据：11:42 evict 掉 5250 个 key 后，主库不降反升到 278MB。

本脚本用**同一份合成面板、同一套 write_partitioned 调用**跑两种模式对照：
  A) 每轮显式 CHECKPOINT（= 我之前的复现，预期有界振荡）
  B) 全程不 CHECKPOINT（= 生产真实行为，验证是否单调增长）

若 B 单调增长而 A 有界，则坐实：**残留膨胀根因 = 生产从不 CHECKPOINT/VACUUM，
DELETE 释放的块不归还文件，每日全量重写(bars 新代 + 财务 TTL 到期)使文件棘轮式上涨。**

用法：
    D:/programs/Python311/python.exe tests/diag_duckdb_checkpoint_effect.py [symbols] [days] [iters]
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


def build_frame(n_symbols: int, n_days: int) -> pd.DataFrame:
    rng = np.random.default_rng(7)
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


def sizes(dbpath: Path) -> tuple[int, int]:
    wal = Path(str(dbpath) + ".wal")
    return dbpath.stat().st_size, (wal.stat().st_size if wal.exists() else 0)


def run_mode(df: pd.DataFrame, iters: int, checkpoint: bool) -> tuple[list, list, int]:
    """跑一种模式，返回 (每轮主库MB, 每轮WAL MB, 最终行数)。"""
    tmp = Path(tempfile.mkdtemp(prefix="ckpt_" + ("A" if checkpoint else "B") + "_"))
    dbpath = tmp / "t.duckdb"
    db = Database(str(dbpath), schema="market")
    repo = MarketRepository(db)
    table = repo._table("bars_cache")

    def key_fn(s):
        return repr((s, "1d", "2025-11-01", "hfq", ("akshare", "qmt"), 3))

    dbs, wals = [], []
    print(f"  {'轮':>3} {'主库MB':>9} {'WAL MB':>8} {'合计MB':>9} {'行数':>10}")
    for it in range(1, iters + 1):
        repo.write_partitioned("bars_cache", df, key_fn=key_fn, partition_col="symbol")
        if checkpoint:
            db.execute("CHECKPOINT")
        d, w = sizes(dbpath)
        cnt = db.scalar(f"SELECT count(*) FROM {table}")
        dbs.append(d / MB)
        wals.append(w / MB)
        print(f"  {it:>3} {d/MB:>9.3f} {w/MB:>8.3f} {(d+w)/MB:>9.3f} {cnt:>10,}")
    final_cnt = db.scalar(f"SELECT count(*) FROM {table}")
    db.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return dbs, wals, final_cnt


def main() -> int:
    n_symbols = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    n_days = int(sys.argv[2]) if len(sys.argv) > 2 else 250
    iters = int(sys.argv[3]) if len(sys.argv) > 3 else 6

    df = build_frame(n_symbols, n_days)
    rows = len(df)
    print(f"合成面板：{n_symbols} × {n_days} = {rows:,} 行（每轮 write_partitioned 全量重写相同 key）\n")

    print("=== 模式 A：每轮显式 CHECKPOINT（复现脚本的行为）===")
    a_db, a_wal, a_cnt = run_mode(df, iters, checkpoint=True)

    print("\n=== 模式 B：全程不 CHECKPOINT（生产真实行为）===")
    b_db, b_wal, b_cnt = run_mode(df, iters, checkpoint=False)

    print("\n" + "=" * 60)
    a_total = [d + w for d, w in zip(a_db, a_wal)]
    b_total = [d + w for d, w in zip(b_db, b_wal)]
    print(f"逻辑行数恒定：A={a_cnt:,}  B={b_cnt:,}（数据没堆积）")
    print(f"A(有checkpoint) 合计MB 首→末：{a_total[0]:.2f} → {a_total[-1]:.2f} "
          f"（极差 {max(a_total)-min(a_total):.2f}）")
    print(f"B(无checkpoint) 合计MB 首→末：{b_total[0]:.2f} → {b_total[-1]:.2f} "
          f"（净增 {b_total[-1]-b_total[0]:+.2f}）")
    b_growth = b_total[-1] - b_total[0]
    a_growth = a_total[-1] - a_total[0]
    per_row = (b_growth * MB / rows) if rows and b_growth > 0 else 0
    print(f"\n每轮物理增量：A={a_growth/max(iters-1,1):+.3f} MB/轮  "
          f"B={b_growth/max(iters-1,1):+.3f} MB/轮")
    if b_growth > a_growth + 1.0:
        print(f"→ 结论坐实：不 CHECKPOINT 时文件单调增长（B≫A），DELETE 空间不回收。")
        if per_row > 0:
            print(f"  每行泄漏 ≈ {per_row:.1f} B；外推全市场 140 万行/次全量重写 "
                  f"≈ {per_row*1_400_000/MB:.1f} MB。")
    else:
        print(f"→ B 未显著大于 A：自动 checkpoint 已足够回收，需另寻增长源。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
