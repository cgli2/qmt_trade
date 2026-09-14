"""Serialized, explicit DuckDB transactions with detached query results.

All handles for a file share one process-local connection and lock. DuckDB's
file lock enforces the process boundary; no read-only runtime bypass exists.
The lock remains held for the entire transaction, including repository calls.
"""
from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value):
        raise ValueError(f"Invalid SQL identifier: {value!r}")
    return '"' + value + '"'


def _prep(params):
    def adapt(v):
        if isinstance(v, (dict, list, tuple)):
            return json.dumps(v, ensure_ascii=False, default=str)
        return v
    if isinstance(params, dict):
        return {k: adapt(v) for k, v in params.items()}
    return [adapt(v) for v in params] if params is not None else []


class Result:
    def __init__(self, conn):
        self.description = conn.description
        self.rows = conn.fetchall() if self.description else []
        self.rowcount = (int(self.rows[0][0]) if self.description and
                         self.description[0][0] == "Count" and self.rows else len(self.rows))

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _Owner:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.pid = os.getpid()
        self.refs = 0
        self.depth = 0
        self.failed = False
        try:
            self.conn = duckdb.connect(path, config={"threads": "2", "memory_limit": "1GB"})
        except duckdb.IOException as exc:
            raise RuntimeError(
                f"无法取得 DuckDB 独占进程所有权：{path}。请通过运行中的 API 访问，"
                "离线维护前停止服务；Uvicorn 只能使用一个 worker。"
            ) from exc


_OWNERS = {}
_OWNERS_LOCK = threading.RLock()


class Database:
    def __init__(self, path=":memory:", *, timeout=30.0, schema="main"):
        self.path = str(Path(path).resolve()) if str(path) != ":memory:" else ":memory:"
        self.timeout = timeout
        self.schema = schema
        identifier(schema)
        self._closed = False
        self._key = os.path.normcase(self.path) if self.path != ":memory:" else object()
        with _OWNERS_LOCK:
            self._owner = _OWNERS.get(self._key)
            if self._owner is None:
                if self.path != ":memory:":
                    Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                    if Path(self.path).exists():
                        try:
                            with open(self.path, "rb") as stream:
                                if stream.read(16) == b"SQLite format 3\x00":
                                    raise RuntimeError("旧 SQLite 文件须先离线迁移并核对，不能直接作为 DuckDB 打开。")
                        except PermissionError as exc:
                            raise RuntimeError("数据库由其他进程持有；请使用 API 或停止服务后维护。") from exc
                self._owner = _Owner(self.path)
                _OWNERS[self._key] = self._owner
            self._owner.refs += 1
        self._lock = self._owner.lock
        with self._lock:
            self._owner.conn.execute(f"CREATE SCHEMA IF NOT EXISTS {identifier(schema)}")

    @contextmanager
    def connection(self):
        with self._lock:
            if self._closed or self._owner.pid != os.getpid():
                raise RuntimeError("Database handle closed or inherited by a compute process")
            conn = self._owner.conn
            conn.execute(f"SET schema = '{self.schema}'")
            yield conn

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            outer = self._owner.depth == 0
            if outer:
                conn.execute("BEGIN TRANSACTION")
                self._owner.failed = False
            self._owner.depth += 1
            try:
                yield self
            except BaseException:
                self._owner.failed = True
                raise
            finally:
                self._owner.depth -= 1
                if outer:
                    failed = self._owner.failed
                    conn.execute("ROLLBACK" if failed else "COMMIT")
                    if failed and not __import__('sys').exc_info()[0]:
                        raise RuntimeError("Transaction aborted by a nested operation")

    def execute(self, sql, params=None):
        with self.connection() as conn:
            try:
                return Result(conn.execute(sql, _prep(params)))
            except BaseException:
                if self._owner.depth:
                    self._owner.failed = True
                raise

    def executemany(self, sql, seq):
        rows = [_prep(p) for p in seq]
        if not rows:
            return 0
        with self.transaction():
            with self.connection() as conn:
                conn.executemany(sql, rows)
        return len(rows)

    def executescript(self, script):
        with self.transaction():
            self.execute(script)

    def query(self, sql, params=None):
        result = self.execute(sql, params)
        names = [d[0] for d in result.description]
        return [dict(zip(names, row)) for row in result.fetchall()]

    def query_one(self, sql, params=None):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql, params=None):
        row = self.query_one(sql, params)
        return next(iter(row.values())) if row else None

    def table_exists(self, table):
        return bool(self.scalar("SELECT count(*) FROM information_schema.tables WHERE table_schema=? AND table_name=?", [self.schema, table]))

    def insert(self, table, data, *, ignore=False, replace=False):
        columns = ','.join(identifier(c) for c in data)
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE" if ignore else "INSERT"
        sql = f"{verb} INTO {identifier(table)} ({columns}) VALUES ({','.join('?' for _ in data)}) RETURNING *"
        rows = self.execute(sql, list(data.values())).fetchall()
        # Explicit inserted-row count: callers never depended on a generated ID.
        return len(rows)

    def insert_ignore(self, table, data):
        return self.insert(table, data, ignore=True)

    def update(self, table, data, where, params=None):
        sets = ','.join(f'{identifier(c)}=?' for c in data)
        return self.execute(f"UPDATE {identifier(table)} SET {sets} WHERE {where}", list(data.values()) + list(params or [])).rowcount

    def delete(self, table, where, params=None):
        return self.execute(f"DELETE FROM {identifier(table)} WHERE {where}", params).rowcount

    def close(self):
        with _OWNERS_LOCK, self._lock:
            if self._closed:
                return
            if self._owner.depth:
                raise RuntimeError("Cannot close database during a transaction")
            self._closed = True
            self._owner.refs -= 1
            if self._owner.refs == 0:
                self._owner.conn.close()
                _OWNERS.pop(self._key, None)
