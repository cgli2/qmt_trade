"""Copy old cache files to a staging DuckDB, preserving source hashes and TTL.

No source is removed; bad or ambiguous files are recorded in quarantine.
Run: python -m scripts.migrations.legacy_files data STAGING.duckdb
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

import pandas as pd

from qmt_trade.storage.db import Database
from qmt_trade.storage.market import MarketRepository


def import_files(root, destination):
    root = Path(root).resolve()
    db = Database(destination, schema="meta")
    market_db = Database(destination, schema="market")
    cache_db = Database(destination, schema="cache")
    stores = {"market": MarketRepository(market_db), "cache": MarketRepository(cache_db)}
    db.execute("CREATE TABLE IF NOT EXISTS legacy_imports (path VARCHAR PRIMARY KEY, sha256 VARCHAR, bytes BIGINT, rows BIGINT, target VARCHAR, status VARCHAR, error VARCHAR, imported DOUBLE)")
    db.execute("CREATE TABLE IF NOT EXISTS legacy_documents (path VARCHAR PRIMARY KEY, payload VARCHAR, source_visible_time VARCHAR)")
    count, bad, row_count = 0, 0, 0
    try:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if not path.is_file() or any(p in {"archive", "db"} for p in relative.parts) or path.suffix not in {".parquet", ".json", ".jsonl"}:
                continue
            fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
            old = db.query_one("SELECT sha256,status FROM legacy_imports WHERE path=?", [str(relative)])
            if old and old["sha256"] == fingerprint and old["status"] == "verified":
                continue
            rows, target, status, error = 0, "", "verified", None
            try:
                if path.suffix == ".parquet":
                    frame = pd.read_parquet(path)
                    category, dataset, key = "market", "legacy_" + relative.parent.as_posix(), path.stem
                    written_at = path.stat().st_mtime
                    if relative.parts[0] == "fundamentals":
                        symbol, table = path.stem.split("__", 1)
                        category, dataset, key = "cache", "qmt_" + table, symbol.replace("_", ".")
                    elif relative.parts[0] == "akshare_cache":
                        tag, key = path.stem.rsplit("_", 1)
                        category, dataset = "cache", "akshare_" + tag
                        if tag == "news":
                            # Preserve source key without guessing stock exchange.
                            category, dataset = "market", "legacy_akshare_news"
                    elif relative.parts[0] == "bars_cache":
                        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
                        key = meta["cache_key"]
                        written_at = float(meta["written_at"])
                        dataset = "bars_cache"
                    elif relative.parts[0] == "parquet" and len(relative.parts) >= 3:
                        dataset = relative.parts[1]
                    store = stores[category]
                    with store.db.transaction():
                        store.write(dataset, frame, key)
                        actual = store.read(dataset, key)
                        # Stable content hashes include column names and all rows;
                        # normalize temporal dtypes to retain old parser semantics.
                        # SQL does not preserve order within equal dates. Verify
                        # the complete row multiset, including duplicate counts.
                        def canonical(value):
                            rows = json.loads(value.to_json(orient="records", date_format="iso", double_precision=15))
                            return sorted(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)
                        before = canonical(frame)
                        after = canonical(actual)
                        if before != after:
                            raise ValueError("完整行内容核对不一致")
                        store.db.update("dataset_coverage", {"written_at": written_at}, "dataset=? AND key=?", [dataset, key])
                    rows, target = len(frame), f"{category}.{dataset}"
                else:
                    payload = path.read_text(encoding="utf-8")
                    parsed = json.loads(payload) if path.suffix == ".json" else [json.loads(line) for line in payload.splitlines() if line.strip()]
                    db.insert("legacy_documents", {"path": str(relative), "payload": payload, "source_visible_time": "unknown"}, replace=True)
                    if path.name == "industry_map_em.json":
                        mapping = parsed.get("map", parsed)
                        market_db.execute("CREATE TABLE IF NOT EXISTS industry_map (symbol VARCHAR PRIMARY KEY, industry VARCHAR, source VARCHAR, visible_time VARCHAR)")
                        market_db.executemany("INSERT OR REPLACE INTO industry_map VALUES (?,?,?,?)", [(str(k), str(v), str(relative), None) for k, v in mapping.items() if v])
                    rows, target = len(parsed) if hasattr(parsed, "__len__") else 1, "meta.legacy_documents"
            except Exception as exc:
                status, error = "quarantined", f"{type(exc).__name__}: {exc}"
                bad += 1
                print(f"quarantine {relative}: {error}", flush=True)
            db.insert("legacy_imports", {"path": str(relative), "sha256": fingerprint, "bytes": path.stat().st_size,
                                        "rows": rows, "target": target, "status": status, "error": error, "imported": time.time()}, replace=True)
            count += 1
            row_count += rows
            if count % 500 == 0:
                print(f"files={count} rows={row_count} quarantined={bad}", flush=True)
        return {"files": count, "rows": row_count, "quarantined": bad}
    finally:
        market_db.close()
        cache_db.close()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("destination")
    args = parser.parse_args()
    print(import_files(args.root, args.destination))
