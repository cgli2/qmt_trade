"""实盘账本假快照清理脚本的回归测试。

对应 2026-09-15 实盘对账 ``CASH_MISMATCH 本地=1000000.0 券商=53608.39``：
8-11 UI 误触 live 时账本为空，``portfolio`` 回落 ``backtest.initial_cash`` 建了
一个 1,000,000 的假基线并被 persist 落库。代码根因已在 app.py 修掉，本组测的是
存量脏数据的清理脚本 —— 它直接删生产账本，判据错一点就是真金白银的事故，
所以每条安全边界都要有测试兜住：

- 干跑绝不写库；
- 只删「现金恰为回测本金且无持仓无市值」的假快照，真实快照必须留下；
- 有真实快照留下时，持仓表绝不能被清空（否则账本自相矛盾）；
- 账本存在真实成交时中止，除非显式 --force；
- 判定基准必须真读到已发布配置，不能因自锁回落到硬编码默认值；
- 删除前必须导出可回溯备份。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb
import pytest

from qmt_trade.storage.configuration import publish
from qmt_trade.storage.db import Database
from qmt_trade.storage.models import Repos
from qmt_trade.storage.runtime import account_schema

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrations" / "reset_live_ledger_baseline.py"
ACCOUNT = "reset-ledger-test-acct"
SCHEMA = account_schema("live", ACCOUNT)
BT_CASH = 1_000_000.0


def _run(db: Path, archive: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--account", ACCOUNT,
         "--archive-dir", str(archive), "--initial-cash", str(BT_CASH), *extra],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")


def _seed(tmp_path: Path, *, snapshots: list[tuple], positions: list[tuple] = (),
          trades: list[str] = ()) -> Path:
    """建一个带真实 DDL 的 DuckDB，塞入指定的 live 账本数据。"""
    db_path = tmp_path / "qmt.duckdb"
    db = Database(str(db_path), schema=SCHEMA)
    repos = Repos.create(db)
    for d, total, cash, mv, cnt in snapshots:
        repos.snapshots.save(date.fromisoformat(d), total_asset=total, cash=cash,
                             market_value=mv, position_count=cnt)
    for sym, vol in positions:
        repos.positions.upsert(sym, volume=vol, available=vol, avg_cost=10.0)
    for sym in trades:
        repos.trades.add(symbol=sym, side="BUY", price=10.0, volume=100,
                         amount=1000.0, trade_date="2026-09-15")
    db.close()
    return db_path


def _snaps(db_path: Path) -> dict[str, float]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            f'SELECT trade_date, cash FROM "{SCHEMA}".account_snapshots ORDER BY trade_date'
        ).fetchall()
    finally:
        con.close()
    return {str(d)[:10]: float(c) for d, c in rows}


def _positions(db_path: Path) -> list[str]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return [r[0] for r in con.execute(
            f'SELECT symbol FROM "{SCHEMA}".positions ORDER BY symbol').fetchall()]
    finally:
        con.close()


# 8-11 误触建的假基线 + 9-15 review job 结转出来的假快照（与生产现场一致）
FICTIONAL = [("2026-08-11", BT_CASH, BT_CASH, 0.0, 0),
             ("2026-09-15", BT_CASH, BT_CASH, 0.0, 0)]


def test_dry_run_does_not_write(tmp_path):
    db = _seed(tmp_path, snapshots=FICTIONAL)
    archive = tmp_path / "archive"
    res = _run(db, archive)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "干跑结束" in res.stdout
    assert _snaps(db) == {"2026-08-11": BT_CASH, "2026-09-15": BT_CASH}
    assert not archive.exists() or not list(archive.glob("*.json"))


def test_apply_removes_only_fictional_and_backs_up(tmp_path):
    db = _seed(tmp_path, snapshots=FICTIONAL)
    archive = tmp_path / "archive"
    res = _run(db, archive, "--apply")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _snaps(db) == {}
    assert "live 账本已空" in res.stdout

    backups = list(archive.glob("live_ledger_reset_*.json"))
    assert len(backups) == 1, f"必须导出可回溯备份，实得 {backups}"
    payload = json.loads(backups[0].read_text(encoding="utf-8"))
    assert payload["schema"] == SCHEMA
    assert sorted(payload["deleted_snapshot_dates"]) == ["2026-08-11", "2026-09-15"]
    assert len(payload["account_snapshots"]) == 2


def test_real_snapshot_and_its_positions_are_preserved(tmp_path):
    """有真快照留下时，持仓绝不能被顺手清空 —— 那会造出
    「快照说有持仓、positions 表却是空的」的矛盾账本，比假现金更难查。"""
    real = ("2026-09-14", 80_000.0, 50_000.0, 30_000.0, 1)
    db = _seed(tmp_path, snapshots=FICTIONAL + [real],
               positions=[("600216.SH", 1000)])
    res = _run(db, tmp_path / "archive", "--apply")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _snaps(db) == {"2026-09-14": 50_000.0}
    assert _positions(db) == ["600216.SH"]
    assert "仍有真快照" in res.stdout


def test_wipes_positions_only_when_ledger_becomes_empty(tmp_path):
    """账本彻底清空时才清持仓 —— 这样重启后 portfolio 走 snap=None 分支，
    才会去采纳券商基线；残留持仓会让它误以为库里已有真值。"""
    db = _seed(tmp_path, snapshots=FICTIONAL, positions=[("300570.SZ", 600)])
    res = _run(db, tmp_path / "archive", "--apply")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _positions(db) == []


def test_aborts_when_real_trades_exist(tmp_path):
    """账本有真实成交 = 有真实盈亏历史，盲删快照会让归因失去基线。"""
    db = _seed(tmp_path, snapshots=FICTIONAL, trades=["600216.SH"])
    archive = tmp_path / "archive"
    res = _run(db, archive)
    assert res.returncode == 4, res.stdout + res.stderr
    assert "中止" in res.stdout
    assert _snaps(db) == {"2026-08-11": BT_CASH, "2026-09-15": BT_CASH}

    forced = _run(db, archive, "--apply", "--force")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert _snaps(db) == {}


def test_noop_when_cash_is_not_backtest_principal(tmp_path):
    """现金不等于回测本金 → 不是这个 bug，脚本必须一行都不删。
    那种情况是「真实成交未入账」，只能走对账人工签核，不能删数据。"""
    db = _seed(tmp_path, snapshots=[("2026-09-15", 60_000.0, 60_000.0, 0.0, 0)])
    res = _run(db, tmp_path / "archive", "--apply")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "无需清理" in res.stdout
    assert _snaps(db) == {"2026-09-15": 60_000.0}


def test_reports_clearly_when_db_is_locked(tmp_path):
    """后端在跑时 DuckDB 是独占锁 —— 必须给出「先停后端」的明确指引，
    而不是抛一屏 traceback 让人以为是脚本坏了。"""
    db = _seed(tmp_path, snapshots=FICTIONAL)
    holder = duckdb.connect(str(db))
    try:
        res = _run(db, tmp_path / "archive")
    finally:
        holder.close()
    assert res.returncode == 3, res.stdout + res.stderr
    assert "停掉后端" in res.stdout
    assert _snaps(db) == {"2026-08-11": BT_CASH, "2026-09-15": BT_CASH}


def test_missing_schema_is_not_an_error(tmp_path):
    """live schema 还不存在（从没跑过实盘）时应正常退出，不报错。"""
    db_path = tmp_path / "qmt.duckdb"
    Database(str(db_path), schema="trading_live_someotherhash").close()
    res = _run(db_path, tmp_path / "archive")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "无需处理" in res.stdout


def test_missing_db_file_exits_cleanly(tmp_path):
    res = _run(tmp_path / "nope.duckdb", tmp_path / "archive")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "找不到 runtime DB" in res.stdout


def test_reads_initial_cash_from_published_settings(tmp_path, monkeypatch):
    """判定基准必须真读到配置，不能靠「硬编码默认值刚好等于配置值」。

    脚本自己 ``duckdb.connect(--db)`` 之后，``Settings.load()`` 内部的
    ``read_active`` 会再开同一个文件 —— DuckDB 独占锁下必然失败，只能回落到
    默认的 1,000,000。而生产里 ``--db`` 与 ``runtime_path()`` 就是同一个
    ``data/db/qmt.duckdb``，所以这不是理论问题：一旦有人把
    ``backtest.initial_cash`` 改成别的数，脚本就会拿 1,000,000 去判 ——
    该删的假快照漏删，或反过来把真钱的快照误判成假的删掉。
    """
    published = 250_000.0
    db = _seed(tmp_path, snapshots=[("2026-09-15", published, published, 0.0, 0)])

    # 与生产同构：--db 与 runtime_path() 指向同一个文件，才会触发自锁
    monkeypatch.setenv("QMT_RUNTIME_DB", str(db))
    publish("settings", {"backtest": {"initial_cash": published}})

    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--account", ACCOUNT,
         "--archive-dir", str(tmp_path / "archive")],       # 故意不传 --initial-cash
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "读取 settings 失败" not in res.stdout, res.stdout
    assert f"{published:,.2f}" in res.stdout, res.stdout
    assert "命中假快照 1 行" in res.stdout, res.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
