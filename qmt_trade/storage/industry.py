"""Industry classifications from runtime tables or an isolated input snapshot."""
from .db import Database
from .runtime import runtime_path

_snapshot_mapping = None


def industry_map(source=None):
    if _snapshot_mapping is not None:
        return dict(_snapshot_mapping)
    db = Database(runtime_path(), schema="market")
    try:
        if not db.table_exists("industry_map"):
            return {}
        return {r["symbol"]: r["industry"] for r in db.query("SELECT symbol,industry FROM industry_map")}
    finally:
        db.close()
