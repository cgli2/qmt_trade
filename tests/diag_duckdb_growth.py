"""DuckDB 文件体积增长采样器（只读，不触碰库内容）。

后端是「唯一数据库所有者进程」，独占文件锁，任何进程外读取（连 open() 都
PermissionError）都不可行，也不能在盘中重启实盘后端。因此退一步：只观测
文件体积/修改时间随时间的变化，用于把「膨胀」归因到具体的时间窗口，再与
backend.log 的事件时间线对齐。

用法：
    D:/programs/Python311/python.exe tests/diag_duckdb_growth.py [秒数] [间隔秒]
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "db" / "qmt.duckdb"
WAL = Path(str(DB) + ".wal")


def snap():
    st = DB.stat()
    wal = WAL.stat().st_size if WAL.exists() else 0
    return st.st_size, wal, st.st_mtime


def main() -> int:
    total = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0

    base_db, base_wal, _ = snap()
    t0 = time.time()
    print(f"起点 {datetime.now():%H:%M:%S}  db={base_db/1048576:.3f} MB  "
          f"wal={base_wal/1048576:.3f} MB  合计={(base_db+base_wal)/1048576:.3f} MB")
    print(f"采样 {total:.0f}s，每 {interval:.0f}s 一次\n")

    last_db, last_wal = base_db, base_wal
    peak_growth_per_min = 0.0
    while time.time() - t0 < total:
        time.sleep(interval)
        db, wal, mt = snap()
        el = time.time() - t0
        d_db = (db - last_db) / 1048576
        d_wal = (wal - last_wal) / 1048576
        rate = (db - last_db) / 1048576 / (interval / 60.0) if interval else 0
        peak_growth_per_min = max(peak_growth_per_min, rate)
        flag = ""
        if abs(d_db) > 0.01 or abs(d_wal) > 0.01:
            flag = "  <== 变化"
        print(f"[{el:6.1f}s] {datetime.now():%H:%M:%S}  db={db/1048576:8.3f} MB "
              f"(Δ{d_db:+7.3f})  wal={wal/1048576:7.3f} MB (Δ{d_wal:+7.3f})  "
              f"mtime={datetime.fromtimestamp(mt):%H:%M:%S}{flag}")
        last_db, last_wal = db, wal

    mins = (time.time() - t0) / 60.0
    d_total = (last_db - base_db) / 1048576
    print(f"\n=== 汇总 ===")
    print(f"观测时长        : {mins:.2f} 分钟")
    print(f"主库净增        : {d_total:+.3f} MB")
    print(f"平均增速        : {d_total/mins:+.3f} MB/分钟  ≈ {d_total/mins*60:+.1f} MB/小时"
          f"  ≈ {d_total/mins*1440:+.1f} MB/天")
    print(f"峰值增速        : {peak_growth_per_min:.3f} MB/分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
