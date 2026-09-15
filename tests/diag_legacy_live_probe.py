"""一次性取证：只看 legacy live sqlite 快照的账本关键行（输出收窄）。"""
import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else \
    "data/archive/legacy_sqlite_20260914/trade_live.snapshot.db"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
cur = con.cursor()

tables = [r[0] for r in cur.execute(
    "select name from sqlite_master where type='table' order by name")]
print("tables:", tables)

for t in ("account_snapshots", "positions", "trades", "orders"):
    if t not in tables:
        continue
    n = cur.execute(f"select count(*) from {t}").fetchone()[0]
    print(f"\n[{t}] rows={n}")
    if t == "account_snapshots":
        for row in cur.execute(
            "select trade_date,total_asset,cash,market_value,position_count "
            "from account_snapshots order by trade_date"):
            print("  ", row)
    else:
        cols = [c[1] for c in cur.execute(f"pragma table_info({t})")]
        print("   cols:", cols[:8])

if "system_state" in tables:
    print("\n[system_state]")
    for row in cur.execute(
        "select key,substr(value,1,60) from system_state where key like "
        "'killswitch%' or key like 'job:re%' or key like 'job:review%' order by key"):
        print("  ", row)
con.close()
