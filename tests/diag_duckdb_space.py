"""DuckDB 空间占用诊断脚本（只读，不触碰线上库）。

用途：定位 data/db/qmt.duckdb 膨胀来源 —— 按 schema/表统计行数、按 dataset
统计 key 数量与 key 长度分布（大 Key 问题）、按 system_state 统计超长 value。

做法：把线上 db + wal 复制到临时文件后以 read_only 打开，避免与常驻后端
（唯一数据库所有者进程）争抢文件锁，也绝不写入线上库。

用法：
    D:/programs/Python311/python.exe tests/diag_duckdb_space.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

DB = ROOT / "data" / "db" / "qmt.duckdb"


def _mb(n) -> str:
    return f"{(n or 0) / 1048576:.2f} MB"


def make_copy() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="qmt_diag_"))
    dst = tmp / "copy.duckdb"
    shutil.copyfile(DB, dst)
    wal = Path(str(DB) + ".wal")
    if wal.exists():
        shutil.copyfile(wal, Path(str(dst) + ".wal"))
    return dst


def main() -> int:
    print(f"线上文件: {DB}  size={_mb(DB.stat().st_size)}"
          f"  wal={_mb(Path(str(DB) + '.wal').stat().st_size)}")
    copy = make_copy()
    con = duckdb.connect(str(copy), read_only=True)

    print("\n=== 1. schema / 表 行数 ===")
    tabs = con.execute(
        "SELECT table_schema, table_name FROM duckdb_tables() "
        "WHERE database_name='copy' ORDER BY 1,2").fetchall()
    sizes = []
    for schema, table in tabs:
        try:
            n = con.execute(f'SELECT count(*) FROM "{schema}"."{table}"').fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {schema}.{table} 统计失败: {exc}")
            continue
        est = 0
        if n:
            try:
                cols = [r[0] for r in con.execute(
                    f'DESCRIBE "{schema}"."{table}"').fetchall()]
                sample = con.execute(
                    f'SELECT sum(length(CAST(t.* AS VARCHAR))) FROM '
                    f'(SELECT * FROM "{schema}"."{table}" LIMIT 2000) t').fetchone()[0]
                est = int((sample or 0) / min(n, 2000) * n)
            except Exception:  # noqa: BLE001
                est = 0
        sizes.append((est, schema, table, n))
    for est, schema, table, n in sorted(sizes, reverse=True)[:30]:
        print(f"  {schema:<28} {table:<34} rows={n:<10} est≈{_mb(est)}")

    print("\n=== 2. dataset_coverage：每个 dataset 的 key 数 / key 长度 ===")
    try:
        rows = con.execute(
            "SELECT dataset, count(*) AS keys, min(length(key)) AS min_len, "
            "avg(length(key))::INT AS avg_len, max(length(key)) AS max_len, "
            "sum(length(key)) AS total_key_bytes, "
            "sum(length(columns_json)+length(coalesce(attrs_json,''))) AS meta_bytes "
            "FROM market.dataset_coverage GROUP BY dataset ORDER BY keys DESC").fetchall()
        for r in rows:
            print(f"  {r[0]:<26} keys={r[1]:<7} len(min/avg/max)={r[2]}/{r[3]}/{r[4]:<7} "
                  f"keyBytes={_mb(r[5])} metaBytes={_mb(r[6])}")
    except Exception as exc:  # noqa: BLE001
        print("  查询失败:", exc)

    print("\n=== 3. dataset_* 明细表：行数 / distinct 指纹 / __dataset_key 占用 ===")
    for schema, table in tabs:
        if not table.startswith("dataset_") or table == "dataset_coverage":
            continue
        try:
            n, nd, kb = con.execute(
                f'SELECT count(*), count(DISTINCT "__dataset_key"), '
                f'sum(length("__dataset_key")) FROM "{schema}"."{table}"').fetchone()
            print(f"  {schema}.{table} rows={n} distinct_fp={nd} "
                  f"fp列占用≈{_mb(kb)} 每行≈{(kb or 0) / max(n, 1):.1f}B")
        except Exception as exc:  # noqa: BLE001
            print(f"  {schema}.{table} 查询失败: {exc}")

    print("\n=== 4. bars_cache key 结构分析（start 漂移检测）===")
    try:
        rows = con.execute(
            "SELECT key FROM market.dataset_coverage WHERE dataset='bars_cache'").fetchall()
        keys = [r[0] for r in rows]
        print(f"  bars_cache key 总数={len(keys)}")
        if keys:
            import ast
            import collections
            starts = collections.Counter()
            freqs = collections.Counter()
            adjusts = collections.Counter()
            provs = collections.Counter()
            syms = set()
            for k in keys:
                try:
                    t = ast.literal_eval(k)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(t, tuple) and len(t) >= 4:
                    syms.add(t[0])
                    freqs[t[1]] += 1
                    starts[str(t[2])] += 1
                    adjusts[t[3]] += 1
                    provs[str(t[4])] += 1
            print(f"  distinct symbol={len(syms)}  freq={dict(freqs)}  adjust={dict(adjusts)}")
            print(f"  distinct start={len(starts)}  providers={dict(provs)}")
            for s, c in sorted(starts.items())[:40]:
                print(f"     start={s:<14} keys={c}")
    except Exception as exc:  # noqa: BLE001
        print("  查询失败:", exc)

    print("\n=== 5. system_state 超长 value Top20 ===")
    for schema, table in tabs:
        if table != "system_state":
            continue
        try:
            rows = con.execute(
                f'SELECT key, length(value) AS L, updated_at FROM "{schema}"."{table}" '
                f'ORDER BY L DESC LIMIT 20').fetchall()
            total = con.execute(
                f'SELECT count(*), sum(length(value)) FROM "{schema}"."{table}"').fetchone()
            print(f"  {schema}.system_state rows={total[0]} 总 value 字节≈{_mb(total[1])}")
            for k, L, ua in rows:
                print(f"     {k[:60]:<62} len={_mb(L)} updated_at={ua}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {schema}.system_state 查询失败: {exc}")

    con.close()
    shutil.rmtree(copy.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
