"""重建 runtime DuckDB，回收被 __dataset_key 明文撑爆的空间。

背景：market.bars_cache 的 key 内嵌完整标的列表（全市场硬筛一次 5,219 只 →
67,912 字符），storage/market.py 把它逐行复制到 __dataset_key，而 DuckDB 不对
超长 VARCHAR 做字典压缩（实测 67,974 B/行）。4,680,843 行 × 66 KiB ≈ 295 GiB，
占 qmt.duckdb 的 99%。key_fingerprint() 已改为定长指纹，本脚本负责一次性回收。

丢弃依据：bars_cache 是 12 小时 TTL 的派生缓存（manager.py 的 _BARS_DISK_TTL），
次日 09:35 data_sync 必然重拉，前瞻价值为零。cache schema（基本面/新闻）与全部
账本 schema 一律保留。

用法（必须先停后端，DuckDB 独占文件锁）：
    python -m scripts.migrations.rebuild_runtime_db --db data/db/qmt.duckdb
    python -m scripts.migrations.rebuild_runtime_db --db data/db/qmt.duckdb --apply

不加 --apply 即干跑：只报告将要丢弃/保留什么，不写任何文件。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qmt_trade.storage.market import key_fingerprint  # noqa: E402

# 丢弃清单：(schema, dataset)。dataset 表名由 sha256 派生，见 MarketRepository._table。
DROP_DATASETS = (("market", "bars_cache"),)

MIN_FREE_GIB = 5.0


def dataset_table(dataset: str) -> str:
    return "dataset_" + hashlib.sha256(dataset.encode()).hexdigest()[:24]


def quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


SKIP_SCHEMAS = {"information_schema", "pg_catalog", "temp"}


def catalog_of(con) -> str:
    """当前主库 catalog 名。必须在任何 USE 之前取：USE 之后 current_database() 就变了。"""
    return con.execute("SELECT current_database()").fetchone()[0]


def list_schemas(con, catalog: str) -> list[str]:
    """枚举全部用户 schema。

    不排除 main：main 里若有表同样会被复制，排除在校验之外就成了「复制了但
    没核对行数」的盲区。

    必须按 catalog 过滤：duckdb_schemas() 与 information_schema.schemata 都不
    过滤 catalog，会跨主库 / system / temp 各返回一个 main，于是 main 被数三遍，
    表数也跟着虚高。
    """
    rows = con.execute(
        "SELECT schema_name FROM duckdb_schemas() WHERE database_name=? ORDER BY 1",
        [catalog]).fetchall()
    return sorted(r[0] for r in rows if r[0] not in SKIP_SCHEMAS)


def list_tables(con, catalog: str, schema: str) -> list[str]:
    rows = con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name=? AND schema_name=? "
        "ORDER BY table_name", [catalog, schema]).fetchall()
    return [r[0] for r in rows]


def list_views(con, catalog: str) -> list[tuple[str, str, str]]:
    rows = con.execute(
        "SELECT schema_name, view_name, sql FROM duckdb_views() WHERE database_name=? "
        "AND sql IS NOT NULL AND schema_name NOT IN ('information_schema','pg_catalog') "
        "ORDER BY 1,2", [catalog]).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def list_sequences(con, catalog: str, schema: str) -> list[tuple[str, str]]:
    """该 schema 下的序列 DDL。

    账本库的 job_runs 是 id BIGINT DEFAULT(nextval('job_runs_id_seq')) PRIMARY KEY，
    序列不先建，重放建表 DDL 就报 Catalog Error: Sequence ... does not exist。

    sql 里的 START 已把当前位置烘焙进去（实测 last_value=5 的序列导出为
    START 6），所以原样重放不会重发已用过的 id；但这一点不靠信仰，
    guard_sequences() 会拿引用列的 max() 再对一次账。
    """
    rows = con.execute(
        "SELECT sequence_name, sql FROM duckdb_sequences() WHERE database_name=? "
        "AND schema_name=? AND sql IS NOT NULL ORDER BY 1", [catalog, schema]).fetchall()
    return [(r[0], r[1]) for r in rows]


def list_indexes(con, catalog: str) -> list[tuple[str, str, str, str]]:
    """显式 CREATE INDEX / CREATE UNIQUE INDEX 的 DDL，返回 (schema, table, name, sql)。

    约束（PRIMARY KEY、内联 UNIQUE）背后的 ART 索引不会出现在 duckdb_indexes() 里，
    它们随建表 DDL 一起重建，所以这里不会重复创建。

    sql 是「schema.table」两段名、不带 catalog：实测在 USE 到 rebuild_target 之后
    重放，两段名解析到当前 catalog，索引正好落到目标库而不会污染旧库。
    而 idx_picks_date_sym 是 CREATE UNIQUE INDEX，建表 DDL 里并无对应的 UNIQUE，
    不单独重放就会静默丢掉一条完整性约束。
    """
    rows = con.execute(
        "SELECT schema_name, table_name, index_name, sql FROM duckdb_indexes() "
        "WHERE database_name=? AND sql IS NOT NULL ORDER BY 1,2,3", [catalog]).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def literal(value) -> str:
    """SQL 字符串字面量。ATTACH 的路径是字面量不是标识符，不能用 quote()。"""
    return "'" + str(value).replace("'", "''") + "'"


def columns_of(con, schema: str, table: str) -> list[str]:
    rows = con.execute(f"DESCRIBE {quote(schema)}.{quote(table)}").fetchall()
    return [r[0] for r in rows]


def count_all(con, targets: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    counts = {}
    for schema, table in targets:
        counts[(schema, table)] = con.execute(
            f"SELECT count(*) FROM {quote(schema)}.{quote(table)}").fetchone()[0]
    return counts


def copy_to_new(con, catalog: str, schemas: list[str], keep: list[tuple[str, str]],
                views: list[tuple[str, str, str]],
                indexes: list[tuple[str, str, str, str]], new_path: Path) -> None:
    """逐表重放 duckdb_tables().sql，再 INSERT SELECT 搬数据。源库全程只被 SELECT。

    选型依据：三种机制在同构夹具上实测，PRIMARY KEY / 列类型（含 TIMESTAMP_NS
    纳秒精度）/ 视图 / 全部行内容均逐项一致，但 COPY FROM DATABASE 与
    EXPORT+IMPORT DATABASE 都无法排除指定表，会把 295 GiB 的 bars_cache 一起
    物化，而磁盘只剩 72 GB。只有逐表重放能跳过丢弃表。

    三处不能想当然：
    - DDL 前缀不统一（"cache".dataset_x 带引号 schema、jobs.job_runs 不带、
      main.stray_note 完全无前缀），字符串改写很脆，改为 USE 到目标 schema 后原样重放。
    - PK 必须保住：Database.insert(replace=True) 生成 INSERT OR REPLACE，DuckDB
      表上没有 PK/UNIQUE 会直接报错，所以 CREATE TABLE AS SELECT 这类丢约束的复制不可用。
    - 建对象的顺序是硬要求：序列 → 表（DDL 引用 nextval）→ 数据 → 索引。
      索引放在数据之后，顺便用真实数据再验一次 UNIQUE 是否仍成立。
    """
    # DDL 全部在 USE 之前取完：USE 之后当前 catalog 变成 rebuild_target，
    # 再去查 duckdb_tables() 就是在赌系统函数的解析规则，没必要赌。
    ddls = {}
    for schema, table in keep:
        row = con.execute(
            "SELECT sql FROM duckdb_tables() WHERE database_name=? AND schema_name=? "
            "AND table_name=?", [catalog, schema, table]).fetchone()
        if not row or not row[0]:
            raise RuntimeError(f"取不到建表 DDL：{schema}.{table}")
        ddls[(schema, table)] = row[0]
    seqs = {schema: found for schema in schemas
            if (found := list_sequences(con, catalog, schema))}

    if new_path.exists():
        new_path.unlink()
    con.execute(f"ATTACH {literal(new_path.as_posix())} AS rebuild_target")
    try:
        for schema in dict.fromkeys([s for s in schemas] + [s for s, _, _ in views]):
            con.execute(f"CREATE SCHEMA IF NOT EXISTS rebuild_target.{quote(schema)}")
        for schema in schemas:
            con.execute(f"USE rebuild_target.{quote(schema)}")
            for _name, ddl in seqs.get(schema, []):
                con.execute(ddl)        # 序列先于表：表 DDL 里的 DEFAULT 依赖它
            for table in list_tables(con, catalog, schema):
                if (schema, table) in ddls:
                    con.execute(ddls[(schema, table)])
        for schema, _, ddl in views:
            con.execute(f"USE rebuild_target.{quote(schema)}")
            con.execute(ddl)
        for schema, table in keep:
            # 必须三段全限定：此时 USE 已指向 rebuild_target，两段名会被解析成
            # 目标表（实测变成自己插自己，搬过去 0 行）。
            con.execute(f"INSERT INTO rebuild_target.{quote(schema)}.{quote(table)} "
                        f"SELECT * FROM {quote(catalog)}.{quote(schema)}.{quote(table)}")
        for schema, _table, _name, ddl in indexes:
            con.execute(f"USE rebuild_target.{quote(schema)}")
            con.execute(ddl)
        con.execute("CHECKPOINT rebuild_target")
    finally:
        # 不能 DETACH 当前 catalog，必须先切回源库。
        con.execute(f"USE {quote(catalog)}.main")
        con.execute("DETACH rebuild_target")
    log(f"已复制 {len(keep)} 张表 / {len(views)} 个视图 / "
        f"{sum(len(v) for v in seqs.values())} 个序列 / {len(indexes)} 个索引到 {new_path.name}")


_NEXTVAL_RE = re.compile(r"nextval\(\s*'([^']+)'\s*\)")


def guard_sequences(con, catalog: str) -> None:
    """对账：重建后的序列不得重发已用过的 id，不够就抬高。

    job_runs.id 是 BIGINT DEFAULT nextval(...) PRIMARY KEY，序列位置一旦回退，
    后端下一次写 job_runs 就撞主键 —— 而这种损坏要等到重启后第一次落库才暴露。

    位置读 start_value：新库的序列刚建、还没被 nextval 过，此时 start_value 就是
    下一个要发的值（实测：CREATE 后未使用时 start_value == DDL 里的 START）。
    抬高只能 CREATE OR REPLACE：setval 不存在，ALTER SEQUENCE ... RESTART WITH 在
    DuckDB 1.4.4 报 Not implemented。
    """
    users: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for schema, table, column, default in con.execute(
            "SELECT schema_name, table_name, column_name, column_default "
            "FROM duckdb_columns() WHERE database_name=? AND column_default LIKE '%nextval%' "
            "ORDER BY 1,2,3", [catalog]).fetchall():
        match = _NEXTVAL_RE.search(default or "")
        if match:
            users.setdefault((schema, match.group(1)), []).append((table, column))

    for (schema, seq), refs in sorted(users.items()):
        row = con.execute(
            "SELECT start_value, increment_by, min_value, max_value, cycle "
            "FROM duckdb_sequences() WHERE database_name=? AND schema_name=? AND sequence_name=?",
            [catalog, schema, seq]).fetchone()
        if not row:
            raise RuntimeError(f"{schema}.{seq} 被 {refs} 引用，但重建后不存在")
        start, increment, min_value, max_value, cycle = row
        for table, column in refs:
            used = con.execute(
                f"SELECT max({quote(column)}) FROM {quote(schema)}.{quote(table)}").fetchone()[0]
            if used is None:
                continue
            if start > used:
                log(f"  序列 {schema}.{seq} 位置正常：下一个 {start} > {table}.{column} 的 max {used}")
                continue
            restart = int(used) + int(increment)
            con.execute(
                f"CREATE OR REPLACE SEQUENCE {quote(schema)}.{quote(seq)} "
                f"INCREMENT BY {int(increment)} MINVALUE {int(min_value)} "
                f"MAXVALUE {int(max_value)} START {restart} "
                f"{'CYCLE' if cycle else 'NO CYCLE'}")
            log(f"  !! 序列 {schema}.{seq} 位置 {start} 未超过 {table}.{column} 的 max {used}，"
                f"已抬高到 START {restart}")
            start = restart


def migrate_fingerprints(con, schema: str, table: str, known: set[str]) -> dict:
    """把一张 dataset 表的 __dataset_key 明文改写为定长指纹。

    用「建新表 + 行数相等校验 + 改名」而非 UPDATE：行数不等就说明有行的 key
    不在映射表里，直接失败，绝不留下半迁移状态。

    known 是该 schema 下 dataset_coverage 里全部明文 key 的指纹集。落在该集
    里的值说明已经是指纹（混合状态），按恒等映射保留；否则当明文重新指纹。
    不靠「长度 32 且全 hex」猜，因为那会把真的 32 字符明文 key 误判。
    """
    qualified = f"{quote(schema)}.{quote(table)}"
    col = quote("__dataset_key")
    if "__dataset_key" not in columns_of(con, schema, table):
        return {"table": table, "skipped": "无 __dataset_key 列"}

    keys = [r[0] for r in con.execute(f"SELECT DISTINCT {col} FROM {qualified}").fetchall()]
    pairs, already = [], 0
    seen = {}
    for key in keys:
        if key in known:
            fingerprint, already = key, already + 1
        else:
            fingerprint = key_fingerprint(key)
        if fingerprint in seen and seen[fingerprint] != key:
            raise RuntimeError(f"{schema}.{table} 指纹碰撞：{seen[fingerprint]!r} 与 {key!r}")
        seen[fingerprint] = key
        pairs.append((key, fingerprint))

    total = con.execute(f"SELECT count(*) FROM {qualified}").fetchone()[0]
    report = {"table": table, "rows": total, "keys": len(keys),
              "hashed": len(pairs) - already, "already_fingerprint": already}
    if not pairs:
        return report

    con.execute("CREATE OR REPLACE TEMP TABLE _fp_map (plain VARCHAR, fp VARCHAR)")
    con.executemany("INSERT INTO _fp_map VALUES (?,?)", pairs)

    # 保持原有列序：__dataset_key 在原地替换为指纹，不挪到末尾。
    cols = columns_of(con, schema, table)
    select = ",".join(f"m.fp AS {col}" if c == "__dataset_key" else f"o.{quote(c)}" for c in cols)
    staged_name = table + "__fp_staging"
    staging = f"{quote(schema)}.{quote(staged_name)}"
    con.execute(f"DROP TABLE IF EXISTS {staging}")
    con.execute(
        f"CREATE TABLE {staging} AS SELECT {select} "
        f"FROM {qualified} o JOIN _fp_map m ON o.{col} = m.plain")
    # CTAS 遇到重名列不报错，会静默改名成 x_1（实测多出过一列 __dataset_key_1）。
    # read() 按 columns_json 选列，看不见这个多出来的列，只能在此显式核对列名与列序。
    staged = columns_of(con, schema, staged_name)
    if staged != cols:
        con.execute(f"DROP TABLE IF EXISTS {staging}")
        raise RuntimeError(f"{schema}.{table} 迁移后列不一致：{cols} -> {staged}")
    moved = con.execute(f"SELECT count(*) FROM {staging}").fetchone()[0]
    if moved != total:
        con.execute(f"DROP TABLE IF EXISTS {staging}")
        raise RuntimeError(f"{schema}.{table} 迁移行数不符：原 {total} 行，映射后 {moved} 行")
    con.execute(f"DROP TABLE {qualified}")
    con.execute(f"ALTER TABLE {staging} RENAME TO {quote(table)}")
    report["moved"] = moved
    return report


def rebuild(db_path: Path, apply: bool) -> int:
    if not db_path.exists():
        log(f"数据库不存在：{db_path}")
        return 2
    free_gib = shutil.disk_usage(db_path.parent).free / 1024 ** 3
    old_gib = db_path.stat().st_size / 1024 ** 3
    log(f"目标 {db_path}  体积 {old_gib:.2f} GiB  同盘剩余 {free_gib:.2f} GiB")
    if free_gib < MIN_FREE_GIB:
        log(f"剩余空间不足 {MIN_FREE_GIB} GiB，中止")
        return 2

    # 干跑必须是纯只读的，所以 .new 的清理放在 copy_to_new 里，不在这里做。
    new_path = Path(str(db_path) + ".new")

    # 打开失败（IOException）即说明后端仍持锁 —— 这是期望的前置校验。
    con = duckdb.connect(str(db_path), config={"threads": "4", "memory_limit": "4GB"})
    try:
        con.execute("PRAGMA disable_progress_bar")
        catalog = catalog_of(con)
        dropped = {(s, dataset_table(d)) for s, d in DROP_DATASETS}
        schemas = list_schemas(con, catalog)
        log(f"catalog={catalog}  schema：{', '.join(schemas)}")

        all_tables = [(s, t) for s in schemas for t in list_tables(con, catalog, s)]
        keep = [(s, t) for s, t in all_tables if (s, t) not in dropped]
        views = list_views(con, catalog)
        indexes = list_indexes(con, catalog)
        log(f"共 {len(all_tables)} 张表 / {len(views)} 个视图 / {len(indexes)} 个索引；"
            f"丢弃 {len(all_tables) - len(keep)} 张，保留 {len(keep)} 张")

        kept = set(keep)
        expected_indexes = {(s, t, n) for s, t, n, _ in indexes if (s, t) in kept}
        # 被丢弃表上的索引本就应当跟着消失，但必须说出来而不是默默少建。
        lost_indexes = sorted({(s, t, n) for s, t, n, _ in indexes if (s, t) not in kept})
        if lost_indexes:
            log(f"  随丢弃表一并消失的索引 {len(lost_indexes)} 个：{lost_indexes}")
        # migrate_fingerprints 走 DROP + CTAS + RENAME，表上的显式索引会跟着 DROP 没。
        # 实测生产库 26 个索引全在 trading_* 上、dataset_* 表一个都没有，但不能把
        # 「今天刚好没有」当成「以后也不会有」，所以在这里把它变成显式失败。
        migrating = {(s, t) for s, t in keep
                     if t.startswith("dataset_") and t != "dataset_coverage"}
        conflict = sorted({(s, t, n) for s, t, n in expected_indexes if (s, t) in migrating})
        if conflict:
            log(f"中止：待指纹迁移的表上有显式索引，DROP+CTAS 会静默丢掉它们：{conflict}")
            return 2

        coverage_dropped = {}
        for schema, dataset in DROP_DATASETS:
            table = dataset_table(dataset)
            exists = (schema, table) in all_tables
            rows = key_chars = 0
            if table_exists(con, catalog, schema, "dataset_coverage"):
                rows, key_chars = con.execute(
                    f"SELECT count(*), coalesce(sum(length({quote('key')})),0) "
                    f"FROM {quote(schema)}.dataset_coverage WHERE dataset=?", [dataset]).fetchone()
                if exists:
                    coverage_dropped[(schema, "dataset_coverage")] = \
                        coverage_dropped.get((schema, "dataset_coverage"), 0) + rows
            log(f"  丢弃 {schema}.{table}（{dataset}）  存在={exists}  "
                f"coverage 行数={rows}  key 总字符={key_chars}")

        before = count_all(con, keep)
        for target, rows in coverage_dropped.items():
            before[target] -= rows      # 这些 coverage 记录会在新库上删掉
        log(f"保留表行数合计 {sum(before.values()):,}（重建后必须逐表一致）")

        if not apply:
            log("干跑结束：未写任何文件。加 --apply 才执行。")
            return 0

        copy_to_new(con, catalog, schemas, keep, views, indexes, new_path)
    finally:
        con.close()

    new_gib = new_path.stat().st_size / 1024 ** 3
    log(f"新库 {new_gib * 1024:.1f} MiB（旧 {old_gib:.2f} GiB）")

    # 在新库上核对行数并迁移指纹；任何一步不符就保留旧库不动。
    con = duckdb.connect(str(new_path), config={"threads": "4", "memory_limit": "4GB"})
    try:
        con.execute("PRAGMA disable_progress_bar")
        new_catalog = catalog_of(con)
        for schema, dataset in DROP_DATASETS:
            if table_exists(con, new_catalog, schema, "dataset_coverage"):
                con.execute(f"DELETE FROM {quote(schema)}.dataset_coverage WHERE dataset=?", [dataset])
                log(f"  已清理新库 {schema}.dataset_coverage 中 {dataset} 的 coverage 记录")
        after = count_all(con, list(before))
        if len(after) != len(before):
            log(f"校验失败：新库缺表，期望 {len(before)} 张，实际 {len(after)} 张")
            return 1
        mismatch = {k: (before[k], after[k]) for k in before if after[k] != before[k]}
        if mismatch:
            log(f"校验失败：行数不符 {mismatch}")
            return 1
        log(f"行数核对通过：{len(after)} 张表，合计 {sum(after.values()):,} 行")

        found_indexes = {(s, t, n) for s, t, n, _ in list_indexes(con, new_catalog)}
        if found_indexes != expected_indexes:
            log(f"校验失败：索引不一致  缺 {sorted(expected_indexes - found_indexes)}  "
                f"多 {sorted(found_indexes - expected_indexes)}")
            return 1
        log(f"索引核对通过：{len(found_indexes)} 个（含 UNIQUE）")

        guard_sequences(con, new_catalog)

        for schema in sorted({s for s, _ in after}):
            known = set()
            if table_exists(con, new_catalog, schema, "dataset_coverage"):
                known = {key_fingerprint(r[0]) for r in con.execute(
                    f"SELECT key FROM {quote(schema)}.dataset_coverage").fetchall()}
            for table in sorted(t for s, t in after if s == schema):
                if not table.startswith("dataset_") or table == "dataset_coverage":
                    continue
                log(f"  指纹迁移 {schema}.{table}: "
                    f"{migrate_fingerprints(con, schema, table, known)}")
        con.execute("CHECKPOINT")
    except Exception:
        con.close()
        log("迁移或校验失败，旧库未改动，新库保留在 .new 供排查")
        raise
    con.close()

    backup = Path(str(db_path) + ".bak")
    if backup.exists():
        backup.unlink()
    db_path.rename(backup)
    for suffix in (".wal", "-wal"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            stale.unlink()
            log(f"已删除残留 WAL {stale.name}")
    new_path.rename(db_path)
    tmp_dir = Path(str(db_path) + ".tmp")
    log(f"旧库已改名为 {backup.name}（{old_gib:.2f} GiB）；新库已落位")
    log(f"后端验证通过后手动删除 {backup.name} 即可回收 "
        f"{old_gib - db_path.stat().st_size / 1024 ** 3:.1f} GiB")
    if tmp_dir.exists():
        log(f"注意：{tmp_dir.name}/ 溢写目录仍在，可一并清理")
    return 0


def table_exists(con, catalog: str, schema: str, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE database_name=? AND schema_name=? AND table_name=?",
        [catalog, schema, table]).fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="目标库路径，默认 runtime_path()")
    parser.add_argument("--apply", action="store_true", help="真正执行；缺省为干跑")
    args = parser.parse_args()
    if args.db:
        db_path = Path(args.db).resolve()
    else:
        from qmt_trade.storage.runtime import runtime_path
        db_path = runtime_path()
    return rebuild(db_path, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
