"""Columnar dataset repository. Filters execute in DuckDB before materialization."""
from __future__ import annotations

import hashlib
import json
import time
import uuid

import pandas as pd

from .db import Database


def quote(name):
    return '"' + str(name).replace('"', '""') + '"'


# 行级分区键指纹长度（hex）。128 bit，单表数千 key 时碰撞概率约 1e-32。
_FINGERPRINT_LEN = 32


def key_fingerprint(key):
    """__dataset_key 的定长指纹。

    DuckDB 不对超长 VARCHAR 做字典压缩：实测 67,912 字符的 key 每行占用
    67,974 B，而 67 字符的短 key 只要 26.8 B。bars_cache 的 key 内嵌完整
    标的列表（全市场硬筛一次就有 5,219 只），一条日线缓存 156 万行，逐行
    复制一份 66 KiB 明文即 105 GB，三条撑爆到 295 GiB。改为定长指纹。

    dataset_coverage.key 仍存明文，list_keys()/metadata() 靠它精确查找；
    指纹只用于 dataset 表内的行级分区，二者一一对应。
    """
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]


class MarketRepository:
    def __init__(self, db: Database):
        self.db = db
        self.db.execute("CREATE TABLE IF NOT EXISTS dataset_coverage (dataset VARCHAR, key VARCHAR, written_at DOUBLE, columns_json VARCHAR, attrs_json VARCHAR, PRIMARY KEY(dataset,key))")

    def _table(self, dataset):
        return "dataset_" + hashlib.sha256(dataset.encode()).hexdigest()[:24]

    def metadata(self, dataset, key):
        return self.db.query_one("SELECT * FROM dataset_coverage WHERE dataset=? AND key=?", [dataset, str(key)])

    def exists(self, dataset, key="_all"):
        return self.metadata(dataset, key) is not None

    def list_keys(self, dataset):
        return [r["key"] for r in self.db.query("SELECT key FROM dataset_coverage WHERE dataset=? ORDER BY key", [dataset])]

    def write(self, dataset, df, key="_all"):
        return self.upsert(dataset, df, key, primary_keys=(), replace=True)

    def upsert(self, dataset, df, key="_all", *, primary_keys=("date",), sort_by=None, replace=False):
        if df is None:
            return
        if "__dataset_key" in df.columns:
            raise ValueError("Reserved dataset column __dataset_key")
        missing = set(primary_keys) - set(df.columns)
        if missing and not df.empty:
            raise ValueError(f"Missing primary key columns: {sorted(missing)}")
        if not len(df.columns):
            return
        frame = df.copy()
        if primary_keys:
            frame = frame.drop_duplicates(list(primary_keys), keep="last")
        if sort_by:
            frame = frame.sort_values(list(sort_by))
        frame["__dataset_key"] = key_fingerprint(key)
        table, view = self._table(dataset), "incoming_" + uuid.uuid4().hex
        with self.db.transaction():
            with self.db.connection() as conn:
                conn.register(view, frame)
                try:
                    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} AS SELECT * FROM {view} WHERE false")
                    existing = {r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()}
                    for name, dtype, *_ in conn.execute(f"DESCRIBE {view}").fetchall():
                        if name not in existing:
                            conn.execute(f"ALTER TABLE {table} ADD COLUMN {quote(name)} {dtype}")
                    if replace:
                        conn.execute(f"DELETE FROM {table} WHERE __dataset_key=?", [key_fingerprint(key)])
                    elif primary_keys:
                        match = " AND ".join(f"old.{quote(k)} IS NOT DISTINCT FROM new.{quote(k)}" for k in primary_keys)
                        conn.execute(f"DELETE FROM {table} old USING {view} new WHERE old.__dataset_key=new.__dataset_key AND {match}")
                    conn.execute(f"INSERT INTO {table} BY NAME SELECT * FROM {view}")
                    self.db.insert("dataset_coverage", {"dataset": dataset, "key": str(key), "written_at": time.time(),
                                   "columns_json": json.dumps(list(df.columns)), "attrs_json": json.dumps(df.attrs, default=str)}, replace=True)
                finally:
                    conn.unregister(view)

    def read(self, dataset, key="_all", *, start=None, end=None, date_col="date", columns=None):
        with self.db.transaction():
            meta = self.metadata(dataset, key)
            if not meta:
                return pd.DataFrame()
            available = json.loads(meta["columns_json"])
            selected = list(columns) if columns is not None else available
            if set(selected) - set(available):
                raise ValueError("Unknown requested dataset columns")
            where, params = ["__dataset_key=?"], [key_fingerprint(key)]
            if start or end:
                if date_col not in available:
                    raise ValueError(f"No date column {date_col}")
                for op, value in ((">=", start), ("<=", end)):
                    if value is not None:
                        where.append(f"CAST({quote(date_col)} AS TIMESTAMP) {op} CAST(? AS TIMESTAMP)")
                        params.append(str(value))
            sql = f"SELECT {','.join(quote(c) for c in selected)} FROM {self._table(dataset)} WHERE {' AND '.join(where)}"
            if date_col in available:
                sql += f" ORDER BY {quote(date_col)}"
            with self.db.connection() as conn:
                result = conn.execute(sql, params).fetchdf()
            result.attrs = json.loads(meta["attrs_json"] or "{}")
            return result

    def read_many(self, dataset, keys, **kwargs):
        frames = [self.read(dataset, k, **kwargs) for k in keys]
        frames = [f for f in frames if not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def read_many_batch(self, dataset, keys, *, ttl_seconds=None, now=None):
        """批量读多个 key，返回 {明文key: DataFrame}（仅含命中且未过期的 key）。

        read_many 逐 key 走事务循环，5k 标的=5k 次事务；b1 按标的分片缓存后一次
        要读几千个 key，故改为两次查询：先按 coverage IN-list 过滤出新鲜 key 及其
        列/attrs 元信息，再按 __dataset_key IN-list 分块取回全部行、groupby 指纹还原。
        ttl_seconds 非空时跳过写入超期的 key（等价于逐 key 的 TTL miss 判定）。
        """
        keys = [str(k) for k in keys]
        if not keys:
            return {}
        now = now if now is not None else time.time()
        table = self._table(dataset)
        fresh = {}
        with self.db.transaction():
            for i in range(0, len(keys), 1000):
                chunk = keys[i:i + 1000]
                placeholders = ",".join("?" for _ in chunk)
                rows = self.db.query(
                    "SELECT key, written_at, columns_json, attrs_json FROM dataset_coverage "
                    f"WHERE dataset=? AND key IN ({placeholders})",
                    [dataset, *chunk],
                )
                for r in rows:
                    if ttl_seconds is not None and now - r["written_at"] > ttl_seconds:
                        continue
                    fresh[r["key"]] = (json.loads(r["columns_json"]),
                                      json.loads(r["attrs_json"] or "{}"))
            if not fresh or not self.db.table_exists(table):
                return {}
            fps = {key_fingerprint(k): k for k in fresh}
            buckets = {k: [] for k in fresh}
            fp_list = list(fps)
            for i in range(0, len(fp_list), 1000):
                chunk = fp_list[i:i + 1000]
                placeholders = ",".join("?" for _ in chunk)
                with self.db.connection() as conn:
                    frame = conn.execute(
                        f"SELECT * FROM {table} WHERE __dataset_key IN ({placeholders})",
                        chunk,
                    ).fetchdf()
                if frame.empty:
                    continue
                for fp, sub in frame.groupby("__dataset_key"):
                    key = fps.get(fp)
                    if key is not None:
                        buckets[key].append(sub)
        result = {}
        for key, subs in buckets.items():
            if not subs:
                continue
            frame = pd.concat(subs, ignore_index=True).drop(columns=["__dataset_key"])
            cols, attrs = fresh[key]
            frame = frame[[c for c in cols if c in frame.columns]]
            if "date" in frame.columns:
                frame = frame.sort_values("date").reset_index(drop=True)
            frame.attrs = attrs
            result[key] = frame
        return result

    def write_partitioned(self, dataset, df, key_fn, partition_col="symbol"):
        """按 partition_col 拆分 df，每组用 key_fn(值) 生成独立 key，单事务分片写入。

        bars_cache 曾把整个股票池塞进单一 key：池一变 key 全变、永不命中、每次全量
        重拉（b1 卡顿根因）。改为每标的一个 key 后，重叠标的跨池复用，只下载真正新增
        的标的，且 key 定长极短。所有分片在一个事务内写入（先按指纹 IN-list DELETE 旧行
        再 INSERT BY NAME），并为每个 key 写一条 coverage 元数据。返回写入的 key 数。
        """
        if df is None or df.empty:
            return 0
        if "__dataset_key" in df.columns:
            raise ValueError("Reserved dataset column __dataset_key")
        if partition_col not in df.columns:
            raise ValueError(f"Missing partition column: {partition_col}")
        key_by_value = {v: str(key_fn(v)) for v in df[partition_col].unique()}
        frame = df.copy()
        frame["__dataset_key"] = [
            key_fingerprint(key_by_value[v]) for v in frame[partition_col]
        ]
        table, view = self._table(dataset), "incoming_" + uuid.uuid4().hex
        columns_json = json.dumps(list(df.columns))  # 不含内部列 __dataset_key
        attrs_json = json.dumps(df.attrs, default=str)
        fps = sorted({key_fingerprint(k) for k in key_by_value.values()})
        with self.db.transaction():
            with self.db.connection() as conn:
                conn.register(view, frame)
                try:
                    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} AS SELECT * FROM {view} WHERE false")
                    existing = {r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()}
                    for name, dtype, *_ in conn.execute(f"DESCRIBE {view}").fetchall():
                        if name not in existing:
                            conn.execute(f"ALTER TABLE {table} ADD COLUMN {quote(name)} {dtype}")
                    for i in range(0, len(fps), 1000):
                        chunk = fps[i:i + 1000]
                        placeholders = ",".join("?" for _ in chunk)
                        conn.execute(f"DELETE FROM {table} WHERE __dataset_key IN ({placeholders})", chunk)
                    conn.execute(f"INSERT INTO {table} BY NAME SELECT * FROM {view}")
                finally:
                    conn.unregister(view)
            written_at = time.time()
            self.db.executemany(
                'INSERT OR REPLACE INTO "dataset_coverage" '
                '("dataset","key","written_at","columns_json","attrs_json") VALUES (?,?,?,?,?)',
                [[dataset, key, written_at, columns_json, attrs_json]
                 for key in key_by_value.values()],
            )
        return len(key_by_value)

    def delete(self, dataset, key="_all"):
        with self.db.transaction():
            if self.exists(dataset, key):
                self.db.delete(self._table(dataset), "__dataset_key=?", [key_fingerprint(key)])
                self.db.delete("dataset_coverage", "dataset=? AND key=?", [dataset, str(key)])

    def evict_expired(self, dataset, ttl_seconds, *, now=None):
        """按写入时间批量淘汰过期缓存 key，返回淘汰的 key 数。

        此前 TTL 只作用于读命中判断（过期即视为 miss），过期行从不删除：
        股票池每变一次就多堆一份全量日线，磁盘只增不减——这是 qmt.duckdb
        膨胀到 314 GiB 的第二成因（第一成因是明文 key 逐行复制，已由指纹修复）。
        这里一次性删除 dataset 表内对应指纹的行 + coverage 元数据。

        __dataset_key 无索引，用 IN-list 单次全表扫描完成删除，避免逐 key O(N^2)；
        SELECT 与两条 DELETE 同处一个事务（进程内锁全程持有），杜绝读写竞态。
        """
        cutoff = (now if now is not None else time.time()) - ttl_seconds
        table = self._table(dataset)
        with self.db.transaction():
            expired = self.db.query(
                "SELECT key FROM dataset_coverage WHERE dataset=? AND written_at < ?",
                [dataset, cutoff],
            )
            if not expired:
                return 0
            if self.db.table_exists(table):
                fingerprints = [key_fingerprint(r["key"]) for r in expired]
                for i in range(0, len(fingerprints), 1000):
                    chunk = fingerprints[i:i + 1000]
                    placeholders = ",".join("?" for _ in chunk)
                    self.db.execute(
                        f"DELETE FROM {table} WHERE __dataset_key IN ({placeholders})",
                        chunk,
                    )
            self.db.delete("dataset_coverage", "dataset=? AND written_at < ?", [dataset, cutoff])
        return len(expired)
