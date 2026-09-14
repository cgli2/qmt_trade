"""Read-only legacy inventory and verified SQLite COPY migration.

Run with ``python -m scripts.migrations.duckdb_migrate inventory data`` or
``... sqlite SOURCE DESTINATION --schema paper``. Never overwrites a target
table or modifies a source. A live source is copied via SQLite's backup API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from qmt_trade.storage.db import Database
from qmt_trade.storage.duckdb.database import identifier


def digest(rows):
    hashes = sorted(hashlib.sha256(json.dumps(list(row), ensure_ascii=False,
                    default=str, separators=(",", ":")).encode()).hexdigest() for row in rows)
    return hashlib.sha256("\n".join(hashes).encode()).hexdigest()


def inventory(root):
    items = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".db", ".json", ".jsonl", ".parquet"):
            continue
        item = {"path": str(path), "bytes": path.stat().st_size,
                "kind": path.suffix, "archive": "archive" in path.parts}
        if path.suffix == ".db":
            try:
                conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    item["tables"] = [{"table": name, "rows": conn.execute(
                        f"SELECT count(*) FROM {identifier(name)}").fetchone()[0]}
                        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                finally:
                    conn.close()
            except Exception as exc:
                item["error"] = str(exc)
        items.append(item)
    return {"files": items, "total_bytes": sum(i["bytes"] for i in items),
            "space_estimate_bytes": sum(i["bytes"] for i in items) * 3,
            "note": "空间估算为源数据三倍，需试迁移测量；归档、测试数据不可自动归入运行库。"}


def migrate_sqlite(source, destination, schema="main"):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError("Source and destination must differ")
    original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    snapshot = sqlite3.connect(":memory:")
    try:
        original.backup(snapshot)
    finally:
        original.close()
    db = Database(destination, schema=schema)
    report = []
    try:
        with db.transaction():
            for (table, ddl) in snapshot.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                if db.table_exists(table):
                    raise ValueError(f"Target table already exists: {schema}.{table}")
                # Preserve source IDs and full values, including old columns.
                import re
                ddl = re.sub(r"\bREAL\b", "DOUBLE", ddl, flags=re.I)
                if "AUTOINCREMENT" in ddl.upper():
                    max_id = snapshot.execute(f"SELECT coalesce(max(id),0) FROM {identifier(table)}").fetchone()[0]
                    seq = identifier(table + "_id_seq")
                    db.execute(f"CREATE SEQUENCE {seq} START {int(max_id) + 1}")
                    ddl = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", f"BIGINT PRIMARY KEY DEFAULT nextval('{table}_id_seq')", ddl, flags=re.I)
                db.execute(ddl)
                cur = snapshot.execute(f"SELECT * FROM {identifier(table)}")
                columns = [c[0] for c in cur.description]
                rows = cur.fetchall()
                if rows:
                    db.executemany(f"INSERT INTO {identifier(table)} VALUES ({','.join('?' for _ in columns)})", rows)
                actual = db.execute(f"SELECT {','.join(identifier(c) for c in columns)} FROM {identifier(table)}").fetchall()
                # SQLite REAL affinity returns floats, matching DuckDB DOUBLE.
                before, after = digest(rows), digest(actual)
                if before != after or len(rows) != len(actual):
                    raise ValueError(f"Data verification failed for {table}")
                report.append({"table": table, "rows": len(rows), "sha256": before,
                               "verified": True, "verification": "完整行多重集合摘要，涵盖主键/状态/金额/数量/时间"})
            for (sql,) in snapshot.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
                db.execute(sql)
    finally:
        snapshot.close()
        db.close()
    return {"source": str(source), "destination": str(destination), "schema": schema, "tables": report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory")
    inv.add_argument("root")
    migrate = sub.add_parser("sqlite")
    migrate.add_argument("source")
    migrate.add_argument("destination")
    migrate.add_argument("--schema", default="main")
    args = parser.parse_args()
    result = inventory(args.root) if args.command == "inventory" else migrate_sqlite(args.source, args.destination, args.schema)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
