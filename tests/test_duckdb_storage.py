"""Storage safety regressions; exclusively isolated databases."""
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys

import pytest

from qmt_trade.storage.db import Database
from qmt_trade.storage.models import Repos


def test_repository_transaction_rolls_back():
    db = Database()
    repos = Repos.create(db)
    with pytest.raises(ValueError):
        with db.transaction():
            repos.system.set("before_error", "must disappear")
            raise ValueError("injected")
    assert repos.system.get("before_error") is None
    with db.transaction():
        repos.system.set("committed", "yes")
    assert repos.system.get("committed") == "yes"
    db.close()


def test_nested_failure_cannot_commit_when_caught():
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="aborted"):
        with db.transaction():
            db.execute("INSERT INTO t VALUES (1)")
            try:
                with db.transaction():
                    raise ValueError("nested")
            except ValueError:
                pass
    assert db.scalar("SELECT count(*) FROM t") == 0
    db.close()


def test_duplicate_key_and_affected_counts():
    db = Database()
    db.execute("CREATE TABLE orders (id VARCHAR PRIMARY KEY, idem VARCHAR UNIQUE, value DOUBLE)")
    assert db.insert_ignore("orders", {"id": "a", "idem": "same", "value": 0.1}) == 1
    assert db.insert_ignore("orders", {"id": "b", "idem": "same", "value": 2}) == 0
    assert db.update("orders", {"value": 1.23}, "id=?", ["a"]) == 1
    assert db.update("orders", {"value": 2}, "id=?", ["missing"]) == 0
    assert db.delete("orders", "id=?", ["a"]) == 1
    db.close()


def test_shared_file_isolation_threads_and_restart(tmp_path):
    path = tmp_path / "runtime.duckdb"
    paper, live = Database(path, schema="paper_a"), Database(path, schema="live_a")
    for db in (paper, live):
        db.execute("CREATE TABLE values_ (id INTEGER PRIMARY KEY, value DOUBLE)")
    def write(i):
        target = paper if i % 2 else live
        with target.transaction():
            target.insert("values_", {"id": i, "value": i / 10})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(80)))
    assert paper.scalar("SELECT count(*) FROM values_ WHERE id % 2=1") == 40
    assert live.scalar("SELECT count(*) FROM values_ WHERE id % 2=0") == 40
    detached = paper.execute("SELECT count(*) FROM values_")
    live.execute("SELECT 7")
    assert detached.fetchone() == (40,)
    paper.close()
    assert live.scalar("SELECT count(*) FROM values_") == 40
    live.close()
    reopened = Database(path, schema="paper_a")
    assert reopened.scalar("SELECT count(*) FROM values_") == 40
    reopened.close()


def test_another_process_cannot_open_runtime(tmp_path):
    path = tmp_path / "runtime.duckdb"
    db = Database(path)
    code = "from qmt_trade.storage.db import Database; Database(__import__('sys').argv[1])"
    result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)
    assert result.returncode != 0
    assert b"RuntimeError" in result.stderr
    db.close()


def test_market_upsert_pushdown_and_metadata():
    import pandas as pd
    from qmt_trade.storage.market import MarketRepository
    db = Database(schema="market")
    repo = MarketRepository(db)
    frame = pd.DataFrame({"date": pd.to_datetime(["2025-01-01", "2025-01-02"]), "close": [10., 11.]})
    frame.attrs = {"source": "fixture"}
    repo.write("bars", frame, "A")
    repo.upsert("bars", pd.DataFrame({"date": pd.to_datetime(["2025-01-02", "2025-01-03"]), "close": [12., 13.]}), "A")
    repo.write("bars", frame, "B")
    result = repo.read("bars", "A", start="2025-01-02", columns=["close"])
    assert list(result["close"]) == [12., 13.]
    assert len(repo.read("bars", "A")) == 3
    assert repo.read("bars", "B").attrs == {"source": "fixture"}
    assert repo.list_keys("bars") == ["A", "B"]
    with pytest.raises(ValueError):
        repo.upsert("bars", pd.DataFrame({"wrong": [1]}), "A")
    assert len(repo.read("bars", "A")) == 3
    db.close()


def test_sqlite_copy_migration_preserves_ids_and_values(tmp_path):
    import sqlite3
    from scripts.migrations.duckdb_migrate import migrate_sqlite
    source, target = tmp_path / "old.db", tmp_path / "new.duckdb"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL, status TEXT UNIQUE)")
    conn.execute("INSERT INTO events VALUES (17,123456.123456,'filled')")
    conn.commit()
    conn.close()
    report = migrate_sqlite(source, target, "trading")
    assert report["tables"][0]["verified"]
    db = Database(target, schema="trading")
    assert db.scalar("SELECT amount FROM events WHERE id=17") == 123456.123456
    db.insert("events", {"amount": 1.1, "status": "new"})
    assert db.scalar("SELECT id FROM events WHERE status='new'") == 18
    db.close()
