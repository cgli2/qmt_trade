"""Evidence gathering for the Gate-3 reconcile "原始资金额不对" report.

Dumps the live ledger baseline that ``Reconciler._local_cash`` /
``_local_positions`` actually read, so the reported local=1000000.0 can be
traced back to its origin instead of guessed at.

Run:  python tests/diag_live_ledger_baseline.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt_trade.storage.runtime import account_schema, runtime_path  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / "config" / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    _load_env()
    acct = os.environ.get("QMT_ACCOUNT_ID")
    schema = account_schema("live", acct)
    path = runtime_path(DATA_DIR)
    print(f"db     = {path}")
    print(f"schema = {schema}  (account={acct})")
    print(f"exists = {path.exists()}")
    if not path.exists():
        return 1

    import duckdb

    try:
        conn = duckdb.connect(str(path), read_only=True)
    except duckdb.IOException as exc:
        print(f"!! 无法只读打开（服务持有独占锁）: {exc}")
        return 2

    def tables() -> list[str]:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=? ORDER BY table_name", [schema]).fetchall()
        return [r[0] for r in rows]

    names = tables()
    print(f"\n-- tables ({len(names)}) --")
    print(", ".join(names))

    def dump(title: str, sql: str) -> None:
        print(f"\n-- {title} --")
        if not conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema=? AND table_name=?",
                [schema, sql.split('"')[1]]).fetchone()[0]:
            print("  (表不存在)")
            return
        try:
            cur = conn.execute(sql)
        except Exception as exc:  # noqa: BLE001
            print(f"  查询失败: {exc}")
            return
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if not rows:
            print("  (0 行)")
            return
        print("  " + " | ".join(cols))
        for r in rows[:30]:
            print("  " + " | ".join("" if v is None else str(v) for v in r))
        if len(rows) > 30:
            print(f"  ... 共 {len(rows)} 行")

    q = lambda t, s="*": f'SELECT {s} FROM "{schema}"."{t}"'  # noqa: E731

    dump("account_snapshots 全部",
         q("account_snapshots") + " ORDER BY trade_date DESC LIMIT 20")
    dump("positions 全部", q("positions"))
    dump("trades 最近 20", q("trades") + " ORDER BY rowid DESC LIMIT 20")
    dump("system_state", q("system_state"))
    dump("reconcile_logs 最近 5",
         q("reconcile_logs", "id,trade_date,passed,created_at")
         + " ORDER BY created_at DESC LIMIT 5")
    dump("risk_events 最近 15",
         q("risk_events", "*") + " ORDER BY rowid DESC LIMIT 15")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
