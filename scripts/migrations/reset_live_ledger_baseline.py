"""清除实盘账本里由回测本金污染出来的假快照，让账本回到「空」状态。

背景（2026-09-15 实盘对账 CASH_MISMATCH 本地=1000000.0 券商=53608.39）：
2026-08-11 Web UI 误触 mode=live，当时 live 账本没有任何快照，
``TradingContext.portfolio`` 便回落 ``backtest.initial_cash``（1,000,000）当起始
现金，``persist_portfolio`` 把这个虚构值写进了 live ``account_snapshots``；
9-13/9-14 迁移把它带进 DuckDB schema，9-15 的 review job 又把它结转成当日快照。
Gate-3 拿它当「本地真值」去比券商真实资产，于是报出 94 万的假差额。

代码侧的根因已修（``app.py::_adopt_broker_baseline``：live 空账本改为采纳券商
真值，券商不可用则拉闸抛错）。本脚本负责清掉存量脏数据 —— 删完后 live 账本为
空，重启后端再跑一次对账（``reconcile_now`` 先刷库，触达 ``ctx.portfolio``）
即会走新的采纳路径按券商真值重建基线。注意光重启不建基线：没有任何路由访问
``ctx.portfolio``，REDUCE_ONLY 下也不会有下单。

安全边界：
- 只删「可证明是假的」快照：cash 恰等于 backtest.initial_cash 且 position_count=0
  且 market_value=0。真实交易产生的快照不会命中。
- 账本里若有真实成交（trades 非空）直接中止：那意味着存在真实盈亏历史，
  盲删会破坏归因，必须人工核对后再决定（``--force`` 才允许继续）。
- 删除前把命中行原样导出到 data/archive/，可回溯。

用法（必须先停后端，DuckDB 独占文件锁）：
    python -m scripts.migrations.reset_live_ledger_baseline
    python -m scripts.migrations.reset_live_ledger_baseline --apply

不加 --apply 即干跑：只报告将要删除什么，不写任何数据。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qmt_trade.core.config import PROJECT_ROOT, load_dotenv   # noqa: E402
from qmt_trade.storage.runtime import account_schema, runtime_path  # noqa: E402

TOLERANCE = 0.01


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def initial_cash_from_settings() -> float:
    """回测本金。读配置失败时按 1,000,000 兜底（与 settings.yaml 现值一致）。"""
    try:
        from qmt_trade.core.config import Settings
        return float(Settings.load(env_overlay=False)
                     .get("backtest.initial_cash", 1_000_000) or 0)
    except Exception as exc:                              # noqa: BLE001
        log(f"读取 settings 失败（{exc}），按默认 1000000 判定")
        return 1_000_000.0


def table_exists(con, schema: str, table: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?", (schema, table)).fetchone()
    return bool(row and row[0])


def dump(con, schema: str, table: str, limit: int = 200) -> list[dict]:
    if not table_exists(con, schema, table):
        return []
    cur = con.execute(f"SELECT * FROM {quote(schema)}.{quote(table)} LIMIT {int(limit)}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None, help="runtime DuckDB 路径（默认 data/db/qmt.duckdb）")
    ap.add_argument("--account", default=None, help="实盘资金账号（默认取 QMT_ACCOUNT_ID）")
    ap.add_argument("--apply", action="store_true", help="真正执行删除；不加则干跑")
    ap.add_argument("--force", action="store_true",
                    help="账本存在真实成交时仍继续（默认中止）")
    ap.add_argument("--archive-dir", default=None,
                    help="备份导出目录（默认 data/archive）")
    ap.add_argument("--initial-cash", type=float, default=None,
                    help="覆盖判定假快照用的回测本金（默认读 settings.yaml）")
    args = ap.parse_args()

    load_dotenv(PROJECT_ROOT / "config" / ".env")
    path = Path(args.db) if args.db else runtime_path()
    if not path.exists():
        log(f"找不到 runtime DB：{path}")
        return 2

    try:
        schema = account_schema("live", args.account)
    except ValueError as exc:
        log(f"无法确定 live schema：{exc}")
        return 2

    log(f"目标库：{path}")
    log(f"live schema：{schema}")

    # 必须在 duckdb.connect 之前读：Settings.load() 内部 read_active 会再开同一个
    # DB 文件，而 DuckDB 是独占锁 —— 生产里 --db 与 runtime_path() 就是同一个
    # data/db/qmt.duckdb，先连库必然自锁，只能回落到硬编码默认值。判定基准不能
    # 靠「默认值刚好等于配置值」的运气：配置一旦被改，该删的漏删、不该删的误删。
    # read_active 用完即 close，放前面不占锁。
    bt_cash = (float(args.initial_cash) if args.initial_cash is not None
               else initial_cash_from_settings())
    log(f"backtest.initial_cash = {bt_cash:,.2f}（判定假快照的基准）")

    try:
        con = duckdb.connect(str(path))
    except Exception as exc:                              # noqa: BLE001
        log(f"打开失败：{exc}")
        log("DuckDB 是独占文件锁 —— 请先停掉后端（scripts/watchdog_backend.sh 也要停），再重跑。")
        return 3

    try:
        if not table_exists(con, schema, "account_snapshots"):
            log(f"schema {schema} 下没有 account_snapshots，无需处理")
            return 0

        snaps = dump(con, schema, "account_snapshots")
        rows = dump(con, schema, "positions")
        trades = dump(con, schema, "trades")
        orders = dump(con, schema, "orders")
        log(f"账本现状：snapshots={len(snaps)} positions={len(rows)} "
            f"trades={len(trades)} orders={len(orders)}")

        for s in snaps:
            log(f"  快照 {s.get('trade_date')} cash={float(s.get('cash') or 0):,.2f} "
                f"total={float(s.get('total_asset') or 0):,.2f} "
                f"mv={float(s.get('market_value') or 0):,.2f} "
                f"持仓数={s.get('position_count')}")

        if trades and not args.force:
            log("中止：trades 表非空，说明账本里有真实成交历史。")
            log("盲删快照会让已实现盈亏失去基线，请先人工核对，确认可丢弃后加 --force。")
            return 4

        # 假快照判据：现金恰为回测本金，且既无持仓也无市值 —— 真实快照不可能这样
        doomed = [
            s for s in snaps
            if abs(float(s.get("cash") or 0) - bt_cash) < TOLERANCE
            and int(s.get("position_count") or 0) == 0
            and abs(float(s.get("market_value") or 0)) < TOLERANCE
        ]
        dates = [str(s.get("trade_date"))[:10] for s in doomed]

        if not doomed:
            log("没有命中假快照判据的行，账本无需清理。")
            if snaps:
                log("提示：现存快照的 cash 不等于回测本金，若仍与券商对不上，"
                    "属于另一类问题（真实成交未入账），请走对账人工签核，不要删数据。")
            return 0

        log(f"命中假快照 {len(doomed)} 行：{', '.join(dates)}")

        if not args.apply:
            log("干跑结束。确认无误后加 --apply 执行删除。")
            return 0

        # 先导出可回溯，再删
        archive_dir = Path(args.archive_dir) if args.archive_dir else PROJECT_ROOT / "data" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = archive_dir / f"live_ledger_reset_{stamp}.json"
        backup.write_text(json.dumps({
            "schema": schema, "db": str(path), "backtest_initial_cash": bt_cash,
            "deleted_snapshot_dates": dates,
            "account_snapshots": doomed,
            "positions": rows, "trades": trades, "orders": orders,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        log(f"已导出待删数据：{backup}")

        # 删完后是否还有「真」快照留下？有就必须连它的持仓一起保住，
        # 否则账本会出现「快照说有 1 只持仓、positions 表却是空的」的自相矛盾，
        # 比原来的假现金更难查。只有账本彻底清空时才顺带清持仓 ——
        # 那样重启后 portfolio 走 snap=None 分支，才会去采纳券商基线。
        survivors = [s for s in snaps if str(s.get("trade_date"))[:10] not in set(dates)]
        wipe_positions = not survivors

        con.execute("BEGIN")
        try:
            marks = ", ".join("'" + d.replace("'", "''") + "'" for d in dates)
            con.execute(
                f"DELETE FROM {quote(schema)}.account_snapshots "
                f"WHERE CAST(trade_date AS VARCHAR) IN ({marks})")
            if wipe_positions:
                con.execute(f"DELETE FROM {quote(schema)}.positions")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        left = dump(con, schema, "account_snapshots")
        left_pos = dump(con, schema, "positions")
        log(f"删除完成。剩余快照 {len(left)} 行，剩余持仓 {len(left_pos)} 行")
        if left:
            for s in left:
                log(f"  保留 {s.get('trade_date')} cash={float(s.get('cash') or 0):,.2f} "
                    f"持仓数={s.get('position_count')}")
            log("仍有真快照 → 持仓一并保留，后端重启会继续以库为准，不会重新采纳券商基线。")
            log("若这些快照与券商仍对不上，请走对账人工签核，不要再删数据。")
        else:
            log("live 账本已空。下一步：")
            log("  1) 重启后端 —— 清掉内存里缓存的旧 portfolio，")
            log("     否则 review/intraday 作业会把假现金重新写回库")
            log("  2) 跑一次对账，它先刷库、发现 live 账本为空即自动按券商真值建基线：")
            log("     curl 'http://127.0.0.1:7099/api/trade/reconcile?mode=live'")
            log("     注意：光重启不会建基线（没有任何路由访问 ctx.portfolio，")
            log("           且 REDUCE_ONLY 下也不会下单），必须由对账这一步触发")
            log("  3) 人工签核解除 REDUCE_ONLY：python -m qmt_trade reconcile --ack \"...\"")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
