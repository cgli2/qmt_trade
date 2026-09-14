"""Runtime location and explicit account scope; no SQLite fallback."""
import hashlib
import os
from pathlib import Path

from .db import Database


class MigrationRequiredError(RuntimeError):
    """The account cannot be served until its legacy ledger is verified."""


def runtime_path(data_dir=None):
    if os.environ.get("QMT_RUNTIME_DB"):
        return Path(os.environ["QMT_RUNTIME_DB"])
    if data_dir is None:
        from ..core.config import PROJECT_ROOT
        data_dir = PROJECT_ROOT / "data"
    return Path(data_dir) / "db" / "qmt.duckdb"


def account_schema(mode, account_id=None):
    if mode not in ("paper", "live"):
        raise ValueError("mode must be paper or live")
    account = account_id or (os.environ.get("QMT_ACCOUNT_ID") if mode == "live" else "paper")
    if not account:
        raise ValueError("实盘账本必须指定 QMT_ACCOUNT_ID")
    return f"trading_{mode}_" + hashlib.sha256(account.encode()).hexdigest()[:16]


def ledger_database(data_dir, mode):
    schema = account_schema(mode)
    db = Database(runtime_path(data_dir), schema=schema)
    old = Path(data_dir) / ("trade_live.db" if mode == "live" else "trade.db")
    if old.exists() and not db.table_exists("system_state"):
        db.close()
        raise MigrationRequiredError(f"账本升级尚未完成：{old.name} → {schema}。服务维护中，请等待迁移校验完成后刷新。")
    return db
