"""SQLite 存储（WAL 模式）。

修正 qmt_etf 的缺陷 #6：它用 CSV + JSON 存交易记录，没有事务、没有唯一约束，
崩溃时容易写坏。这里改用 SQLite，并给幂等键加**唯一索引**——这是 OrderGuard
幂等保证的最后一道防线（进程崩溃重启后依然有效）。

ADR-5 预留 Postgres：所有 SQL 都走 :meth:`Database.execute`，未使用 SQLite 方言特性
（除 ``INSERT OR IGNORE``，已封装为 :meth:`insert_ignore`）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..core.logging import get_logger

logger = get_logger("storage.db")


def _adapt(value: Any) -> Any:
    """把 Python 对象转成 SQLite 可存类型。dict/list 自动 JSON 序列化。"""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, bool):
        return int(value)
    if value is None or isinstance(value, (int, float, str, bytes)):
        return value
    return str(value)


class Database:
    """线程安全的 SQLite 封装。每个线程持有独立连接。"""

    def __init__(self, path: str | Path = ":memory:", *, timeout: float = 30.0):
        self.path = str(path)
        self.timeout = timeout
        self._local = threading.local()
        self._shared: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if self.path == ":memory:":
            # 内存库不能跨连接共享，统一用一个连接 + 锁
            self._shared = self._new_connection()
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 连接
    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.timeout, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        with self._lock:
            if self._shared is not None:
                self._shared.close()
                self._shared = None
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None

    # ------------------------------------------------------------ 执行
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self.conn
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def execute(self, sql: str, params: Sequence | dict | None = None) -> sqlite3.Cursor:
        with self._lock:
            conn = self.conn
            # 先结束可能残留的读事务（WAL 下旧快照会让写立刻报
            # SQLITE_BUSY_SNAPSHOT，busy_timeout 对这种冲突无效），
            # 保证每条写语句都以全新快照开始。
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            try:
                cur = conn.execute(sql, _prep(params))
                conn.commit()
                return cur
            except Exception:
                conn.rollback()
                raise

    def executemany(self, sql: str, seq: Iterable[Sequence | dict]) -> int:
        rows = [_prep(p) for p in seq]
        if not rows:
            return 0
        with self._lock:
            conn = self.conn
            cur = conn.executemany(sql, rows)
            conn.commit()
            return cur.rowcount

    def executescript(self, script: str) -> None:
        with self._lock:
            conn = self.conn
            conn.executescript(script)
            conn.commit()

    def query(self, sql: str, params: Sequence | dict | None = None) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(sql, _prep(params))
            return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence | dict | None = None) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence | dict | None = None) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return None
        return next(iter(row.values()))

    # ------------------------------------------------------------ 便捷写入
    def insert(self, table: str, data: dict, *, ignore: bool = False, replace: bool = False) -> int:
        cols = list(data.keys())
        placeholders = ",".join("?" for _ in cols)
        verb = "INSERT"
        if replace:
            verb = "INSERT OR REPLACE"
        elif ignore:
            verb = "INSERT OR IGNORE"
        sql = f'{verb} INTO {table} ({",".join(cols)}) VALUES ({placeholders})'
        cur = self.execute(sql, [data[c] for c in cols])
        return int(cur.lastrowid or 0)

    def insert_ignore(self, table: str, data: dict) -> int:
        """插入，冲突则忽略。返回 rowid，冲突时返回 0。"""
        cols = list(data.keys())
        placeholders = ",".join("?" for _ in cols)
        sql = f'INSERT OR IGNORE INTO {table} ({",".join(cols)}) VALUES ({placeholders})'
        with self._lock:
            conn = self.conn
            cur = conn.execute(sql, [_adapt(data[c]) for c in cols])
            conn.commit()
            return int(cur.lastrowid or 0) if cur.rowcount > 0 else 0

    def update(self, table: str, data: dict, where: str, params: Sequence | None = None) -> int:
        sets = ",".join(f"{k}=?" for k in data)
        sql = f"UPDATE {table} SET {sets} WHERE {where}"
        args = [_adapt(v) for v in data.values()] + list(_prep(params) or [])
        cur = self.execute(sql, args)
        return cur.rowcount

    def delete(self, table: str, where: str, params: Sequence | None = None) -> int:
        cur = self.execute(f"DELETE FROM {table} WHERE {where}", params)
        return cur.rowcount

    def table_exists(self, table: str) -> bool:
        return bool(
            self.query_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
        )


def _prep(params: Sequence | dict | None):
    if params is None:
        return ()
    if isinstance(params, dict):
        return {k: _adapt(v) for k, v in params.items()}
    return [_adapt(p) for p in params]
