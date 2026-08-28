"""数据库 Schema 与仓储层。

设计原则 P6（一切可回放、可审计）的落地：决策链路上的每一环——意图、计划、
风控判定、订单、成交、LLM 调用——全部落库，且用 ``trace_id`` 串联。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..core.logging import get_logger
from .db import Database

logger = get_logger("storage.models")

SCHEMA = """
-- 交易意图（LLM 输出）
CREATE TABLE IF NOT EXISTS intents (
    id              TEXT PRIMARY KEY,
    trade_date      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    action          TEXT NOT NULL,
    confidence      REAL,
    conviction      TEXT,
    payload         TEXT NOT NULL,      -- TradeIntent 完整 JSON
    prompt_hash     TEXT,
    trace_id        TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intents_date ON intents(trade_date, symbol);

-- 每日最终精选（多 Agent 投票选出的 3~5 只高胜率标的，含选中理由）
CREATE TABLE IF NOT EXISTS daily_picks (
    id            TEXT PRIMARY KEY,
    trade_date    TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    rank          INTEGER NOT NULL DEFAULT 0,
    action        TEXT NOT NULL,
    conviction    TEXT,
    confidence    REAL,
    industry      TEXT,
    reason        TEXT,
    votes         TEXT,
    intent_id     TEXT,
    payload       TEXT NOT NULL,      -- TradeIntent 完整 JSON
    bull_case     TEXT,               -- 多方核心论据（多空辩论提炼）
    bear_case     TEXT,               -- 空方核心论据
    debate        TEXT,               -- 多空辩论结构化记录(JSON)
    evidence      TEXT,               -- 支撑证据：因子分位+关键原值(JSON)
    created_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_picks_date_sym ON daily_picks(trade_date, symbol);
CREATE INDEX IF NOT EXISTS idx_picks_symbol ON daily_picks(symbol, trade_date);

-- 交易计划（Intent 经风控+仓位后的可执行计划）
CREATE TABLE IF NOT EXISTS plans (
    id              TEXT PRIMARY KEY,
    intent_id       TEXT,
    trade_date      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    planned_shares  INTEGER NOT NULL,
    entry_ref_price REAL,
    entry_trigger   TEXT,
    stop_loss_price REAL,
    take_profit     TEXT,
    max_holding_days INTEGER,
    invalidation_checks TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDING',
    payload         TEXT,
    trace_id        TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_date ON plans(trade_date, status);

-- 订单。idempotency_key 唯一索引是防重复下单的最后一道防线
CREATE TABLE IF NOT EXISTS orders (
    id                TEXT PRIMARY KEY,
    idempotency_key   TEXT NOT NULL UNIQUE,
    plan_id           TEXT,
    trade_date        TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    order_type        TEXT NOT NULL,
    price             REAL,
    volume            INTEGER NOT NULL,
    filled_volume     INTEGER NOT NULL DEFAULT 0,
    avg_fill_price    REAL,
    status            TEXT NOT NULL,
    gateway_order_id  TEXT,
    signal            TEXT,
    reject_reason     TEXT,
    trace_id          TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(trade_date, symbol);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- 成交流水
CREATE TABLE IF NOT EXISTS trades (
    id            TEXT PRIMARY KEY,
    order_id      TEXT,
    trade_date    TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    price         REAL NOT NULL,
    volume        INTEGER NOT NULL,
    amount        REAL NOT NULL,
    commission    REAL NOT NULL DEFAULT 0,
    stamp_duty    REAL NOT NULL DEFAULT 0,
    transfer_fee  REAL NOT NULL DEFAULT 0,
    slippage_cost REAL NOT NULL DEFAULT 0,
    total_cost    REAL NOT NULL DEFAULT 0,
    realized_pnl  REAL,
    traded_at     REAL NOT NULL,
    trace_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trade_date, symbol);

-- 持仓（本地视图，每日与券商对账）
CREATE TABLE IF NOT EXISTS positions (
    symbol          TEXT PRIMARY KEY,
    volume          INTEGER NOT NULL DEFAULT 0,
    available       INTEGER NOT NULL DEFAULT 0,
    avg_cost        REAL NOT NULL DEFAULT 0,
    last_price      REAL NOT NULL DEFAULT 0,
    entry_date      TEXT,
    highest_price   REAL NOT NULL DEFAULT 0,
    stop_loss_price REAL,
    stop_loss_type  TEXT,
    take_profit     TEXT,
    max_holding_days INTEGER,
    invalidation_checks TEXT,
    tp_done_levels  TEXT DEFAULT '[]',
    origin_shares   INTEGER DEFAULT 0,
    plan_id         TEXT,
    industry        TEXT,
    updated_at      REAL NOT NULL
);

-- 账户每日快照
CREATE TABLE IF NOT EXISTS account_snapshots (
    trade_date    TEXT PRIMARY KEY,
    total_asset   REAL NOT NULL,
    cash          REAL NOT NULL,
    market_value  REAL NOT NULL,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    position_count INTEGER NOT NULL DEFAULT 0,
    regime        TEXT,
    created_at    REAL NOT NULL
);

-- 风控事件
CREATE TABLE IF NOT EXISTS risk_events (
    id          TEXT PRIMARY KEY,
    trade_date  TEXT NOT NULL,
    gate        TEXT NOT NULL,
    rule        TEXT NOT NULL,
    symbol      TEXT,
    severity    TEXT NOT NULL DEFAULT 'INFO',
    message     TEXT,
    detail      TEXT,
    trace_id    TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_date ON risk_events(trade_date, gate);

-- LLM 调用记录（缓存 + 成本核算 + 回测重放，三合一）
CREATE TABLE IF NOT EXISTS llm_calls (
    prompt_hash   TEXT PRIMARY KEY,
    model         TEXT NOT NULL,
    node          TEXT,
    symbol        TEXT,
    prompt        TEXT NOT NULL,
    response      TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_cny      REAL NOT NULL DEFAULT 0,
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    trade_date    TEXT,
    trace_id      TEXT,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_date ON llm_calls(trade_date);

-- 经验库（复盘产出，供后续决策检索）
CREATE TABLE IF NOT EXISTS experiences (
    id            TEXT PRIMARY KEY,
    trade_date    TEXT NOT NULL,
    symbol        TEXT,
    situation     TEXT NOT NULL,
    action        TEXT,
    outcome       TEXT,
    pnl_pct       REAL,
    lesson        TEXT NOT NULL,
    tags          TEXT,
    embedding     TEXT,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exp_symbol ON experiences(symbol);

-- KillSwitch 状态（持久化，重启后不丢）
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    reason      TEXT,
    updated_at  REAL NOT NULL
);

-- 对账日志
CREATE TABLE IF NOT EXISTS reconcile_logs (
    id          TEXT PRIMARY KEY,
    trade_date  TEXT NOT NULL,
    passed      INTEGER NOT NULL,
    detail      TEXT,
    created_at  REAL NOT NULL
);

-- 任务执行历史（每次调度一行；system_state 只存最后一次，观察期证据链靠这张表）
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name    TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    status      TEXT NOT NULL,          -- OK / FAIL / SKIP
    reason      TEXT,
    elapsed     REAL,
    started_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_runs_date ON job_runs(trade_date, job_name);

-- 新闻与事件
CREATE TABLE IF NOT EXISTS news (
    id            TEXT PRIMARY KEY,
    symbol        TEXT,
    title         TEXT NOT NULL,
    content       TEXT,
    source        TEXT,
    url           TEXT,
    publish_time  REAL NOT NULL,
    category      TEXT,
    importance    REAL DEFAULT 0,
    sentiment     REAL,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_time ON news(publish_time);
CREATE INDEX IF NOT EXISTS idx_news_symbol ON news(symbol, publish_time);
"""


def init_db(db: Database) -> Database:
    """建表。幂等，可重复调用。"""
    db.executescript(SCHEMA)
    _migrate(db)
    return db


#: 轻量迁移：给老库补新列（CREATE TABLE IF NOT EXISTS 不会更新已存在的表）
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("positions", "stop_loss_type", "TEXT"),
    ("positions", "tp_done_levels", "TEXT DEFAULT '[]'"),
    ("positions", "origin_shares", "INTEGER DEFAULT 0"),
    ("daily_picks", "bull_case", "TEXT"),
    ("daily_picks", "bear_case", "TEXT"),
    ("daily_picks", "debate", "TEXT"),
    ("daily_picks", "evidence", "TEXT"),
]


def _migrate(db: Database) -> None:
    for table, col, typedef in _MIGRATIONS:
        try:
            cols = {r["name"] for r in db.query(f"PRAGMA table_info({table})")}
            if cols and col not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
                logger.info("schema 迁移: %s 增列 %s", table, col)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("schema 迁移失败 %s.%s: %s", table, col, exc)


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:16]
    return f"{prefix}{raw}" if prefix else raw


def _today(d: date | str | None = None) -> str:
    if d is None:
        return date.today().isoformat()
    if isinstance(d, (date, datetime)):
        return d.strftime("%Y-%m-%d")
    return str(d)[:10]


# ==================================================================== 仓储
class BaseRepo:
    def __init__(self, db: Database):
        self.db = db


class OrderRepo(BaseRepo):
    def create(self, **kw) -> str:
        now = time.time()
        oid = kw.pop("id", None) or new_id("ord_")
        row = {
            "id": oid,
            "idempotency_key": kw["idempotency_key"],
            "plan_id": kw.get("plan_id"),
            "trade_date": _today(kw.get("trade_date")),
            "symbol": kw["symbol"],
            "side": kw["side"],
            "order_type": kw.get("order_type", "LIMIT"),
            "price": kw.get("price"),
            "volume": int(kw["volume"]),
            "filled_volume": int(kw.get("filled_volume", 0)),
            "avg_fill_price": kw.get("avg_fill_price"),
            "status": kw.get("status", "PENDING"),
            "gateway_order_id": kw.get("gateway_order_id"),
            "signal": kw.get("signal"),
            "reject_reason": kw.get("reject_reason"),
            "trace_id": kw.get("trace_id"),
            "created_at": now,
            "updated_at": now,
        }
        self.db.insert("orders", row)
        return oid

    def try_reserve(self, idempotency_key: str, **kw) -> str | None:
        """幂等占位：抢占成功返回 order_id，已存在返回 None。

        这是把 qmt_etf 的「下单前置临时 ID」升级为**数据库级唯一约束**，
        即使进程崩溃重启、回调重复触发也不会重复下单。
        """
        oid = new_id("ord_")
        now = time.time()
        row = {
            "id": oid,
            "idempotency_key": idempotency_key,
            "plan_id": kw.get("plan_id"),
            "trade_date": _today(kw.get("trade_date")),
            "symbol": kw["symbol"],
            "side": kw["side"],
            "order_type": kw.get("order_type", "LIMIT"),
            "price": kw.get("price"),
            "volume": int(kw["volume"]),
            "status": "RESERVED",
            "signal": kw.get("signal"),
            "trace_id": kw.get("trace_id"),
            "created_at": now,
            "updated_at": now,
        }
        rowid = self.db.insert_ignore("orders", row)
        return oid if rowid else None

    def update_status(self, order_id: str, status: str, **fields) -> None:
        data = {"status": status, "updated_at": time.time(), **fields}
        self.db.update("orders", data, "id=?", (order_id,))

    def get(self, order_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))

    def get_by_key(self, key: str) -> dict | None:
        return self.db.query_one("SELECT * FROM orders WHERE idempotency_key=?", (key,))

    def get_by_gateway_id(self, gid: str) -> dict | None:
        return self.db.query_one("SELECT * FROM orders WHERE gateway_order_id=?", (str(gid),))

    def list_open(self) -> list[dict]:
        return self.db.query(
            "SELECT * FROM orders WHERE status IN ('RESERVED','PENDING','SUBMITTED','PART_FILLED')"
        )

    def count_today(self, symbol: str, trade_date: date | str | None = None) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM orders WHERE symbol=? AND trade_date=? "
                "AND status NOT IN ('REJECTED','GUARD_BLOCKED')",
                (symbol, _today(trade_date)),
            )
            or 0
        )

    def list_by_date(self, trade_date: date | str) -> list[dict]:
        return self.db.query("SELECT * FROM orders WHERE trade_date=? ORDER BY created_at", (_today(trade_date),))

    def list_recent(self, limit: int = 50) -> list[dict]:
        return self.db.query(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (int(limit),))


class TradeRepo(BaseRepo):
    def add(self, **kw) -> str:
        tid = kw.pop("id", None) or new_id("trd_")
        row = {
            "id": tid,
            "order_id": kw.get("order_id"),
            "trade_date": _today(kw.get("trade_date")),
            "symbol": kw["symbol"],
            "side": kw["side"],
            "price": float(kw["price"]),
            "volume": int(kw["volume"]),
            "amount": float(kw["amount"]),
            "commission": float(kw.get("commission", 0)),
            "stamp_duty": float(kw.get("stamp_duty", 0)),
            "transfer_fee": float(kw.get("transfer_fee", 0)),
            "slippage_cost": float(kw.get("slippage_cost", 0)),
            "total_cost": float(kw.get("total_cost", 0)),
            "realized_pnl": kw.get("realized_pnl"),
            "traded_at": float(kw.get("traded_at", time.time())),
            "trace_id": kw.get("trace_id"),
        }
        self.db.insert("trades", row)
        return tid

    def list_by_symbol(self, symbol: str) -> list[dict]:
        return self.db.query("SELECT * FROM trades WHERE symbol=? ORDER BY traded_at", (symbol,))

    def list_all(self) -> list[dict]:
        return self.db.query("SELECT * FROM trades ORDER BY traded_at")

    def list_by_date(self, trade_date: date | str) -> list[dict]:
        return self.db.query("SELECT * FROM trades WHERE trade_date=? ORDER BY traded_at", (_today(trade_date),))


class PositionRepo(BaseRepo):
    def upsert(self, symbol: str, **fields) -> None:
        fields["symbol"] = symbol
        fields["updated_at"] = time.time()
        self.db.insert("positions", fields, replace=True)

    def patch(self, symbol: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        self.db.update("positions", fields, "symbol=?", (symbol,))

    def get(self, symbol: str) -> dict | None:
        return self.db.query_one("SELECT * FROM positions WHERE symbol=?", (symbol,))

    def list_all(self) -> list[dict]:
        return self.db.query("SELECT * FROM positions WHERE volume > 0")

    def remove(self, symbol: str) -> None:
        self.db.delete("positions", "symbol=?", (symbol,))


class RiskEventRepo(BaseRepo):
    def add(self, gate: str, rule: str, message: str = "", **kw) -> str:
        rid = new_id("rsk_")
        self.db.insert(
            "risk_events",
            {
                "id": rid,
                "trade_date": _today(kw.get("trade_date")),
                "gate": gate,
                "rule": rule,
                "symbol": kw.get("symbol"),
                "severity": kw.get("severity", "INFO"),
                "message": message,
                "detail": kw.get("detail"),
                "trace_id": kw.get("trace_id"),
                "created_at": time.time(),
            },
        )
        return rid

    def list_by_date(self, trade_date: date | str) -> list[dict]:
        return self.db.query("SELECT * FROM risk_events WHERE trade_date=?", (_today(trade_date),))

    def exists(self, rule: str, *, symbol: str | None = None,
               trade_date: date | str | None = None) -> bool:
        """当日是否已有同 rule（同标的）事件，供盘中高频去重。"""
        row = self.db.query_one(
            "SELECT 1 AS x FROM risk_events WHERE rule=? AND trade_date=? AND symbol IS ?",
            (rule, _today(trade_date), symbol))
        return row is not None


class LLMCallRepo(BaseRepo):
    def get(self, prompt_hash: str) -> dict | None:
        return self.db.query_one("SELECT * FROM llm_calls WHERE prompt_hash=?", (prompt_hash,))

    def save(self, **kw) -> None:
        row = {
            "prompt_hash": kw["prompt_hash"],
            "model": kw["model"],
            "node": kw.get("node"),
            "symbol": kw.get("symbol"),
            "prompt": kw["prompt"],
            "response": kw["response"],
            "input_tokens": int(kw.get("input_tokens", 0)),
            "output_tokens": int(kw.get("output_tokens", 0)),
            "cost_cny": float(kw.get("cost_cny", 0.0)),
            "latency_ms": int(kw.get("latency_ms", 0)),
            "trade_date": _today(kw.get("trade_date")),
            "trace_id": kw.get("trace_id"),
            "created_at": time.time(),
        }
        self.db.insert("llm_calls", row, replace=True)

    def cost_since(self, since_date: date | str) -> float:
        return float(
            self.db.scalar(
                "SELECT COALESCE(SUM(cost_cny),0) FROM llm_calls WHERE trade_date >= ?",
                (_today(since_date),),
            )
            or 0.0
        )

    def cost_on(self, trade_date: date | str) -> float:
        return float(
            self.db.scalar(
                "SELECT COALESCE(SUM(cost_cny),0) FROM llm_calls WHERE trade_date = ?",
                (_today(trade_date),),
            )
            or 0.0
        )


class SystemStateRepo(BaseRepo):
    def set(self, key: str, value: str, reason: str = "") -> None:
        self.db.insert(
            "system_state",
            {"key": key, "value": value, "reason": reason, "updated_at": time.time()},
            replace=True,
        )

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.query_one("SELECT value FROM system_state WHERE key=?", (key,))
        return row["value"] if row else default

    def get_row(self, key: str) -> dict | None:
        return self.db.query_one("SELECT * FROM system_state WHERE key=?", (key,))

    def list_keys(self, prefix: str) -> list[str]:
        """按前缀列举 key（复盘 IC 历史、选股截面历史都按日期后缀存）。"""
        rows = self.db.query(
            "SELECT key FROM system_state WHERE key LIKE ? ORDER BY key",
            (prefix + "%",),
        )
        return [r["key"] for r in rows]

    def list_prefix(self, prefix: str) -> dict[str, str]:
        """按前缀取键值对（心跳等批量读取场景，一次查询）。"""
        rows = self.db.query(
            "SELECT key, value FROM system_state WHERE key LIKE ?",
            (prefix + "%",),
        )
        return {r["key"]: r["value"] for r in rows}

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM system_state WHERE key=?", (key,))


class IntentRepo(BaseRepo):
    def add(self, intent: Any, *, trace_id: str | None = None) -> str:
        iid = new_id("int_")
        payload = intent.model_dump_json() if hasattr(intent, "model_dump_json") else str(intent)
        self.db.insert(
            "intents",
            {
                "id": iid,
                "trade_date": _today(getattr(intent, "as_of_date", None)),
                "symbol": getattr(intent, "symbol", ""),
                "action": str(getattr(intent, "action", "")),
                "confidence": float(getattr(intent, "confidence", 0) or 0),
                "conviction": str(getattr(intent, "conviction", "")),
                "payload": payload,
                "prompt_hash": getattr(intent, "prompt_hash", None),
                "trace_id": trace_id,
                "created_at": time.time(),
            },
        )
        return iid

    def list_by_date(self, trade_date: date | str) -> list[dict]:
        return self.db.query("SELECT * FROM intents WHERE trade_date=?", (_today(trade_date),))


class PicksRepo(BaseRepo):
    """每日最终精选。按天覆盖写入，历史可回溯（滚动迭代与命中率跟踪的数据源）。"""

    def add(self, *, trade_date: date | str, symbol: str, payload: str, **kw) -> str:
        pid = new_id("pck_")
        self.db.insert(
            "daily_picks",
            {
                "id": pid,
                "trade_date": _today(trade_date),
                "symbol": symbol,
                "rank": int(kw.get("rank", 0) or 0),
                "action": kw.get("action", "BUY"),
                "conviction": kw.get("conviction"),
                "confidence": kw.get("confidence"),
                "industry": kw.get("industry"),
                "reason": kw.get("reason"),
                "votes": kw.get("votes"),
                "intent_id": kw.get("intent_id"),
                "payload": payload,
                "bull_case": kw.get("bull_case"),
                "bear_case": kw.get("bear_case"),
                "debate": kw.get("debate"),
                "evidence": kw.get("evidence"),
                "created_at": time.time(),
            },
            replace=True,
        )
        return pid

    def clear_date(self, trade_date: date | str) -> None:
        self.db.delete("daily_picks", "trade_date=?", (_today(trade_date),))

    def list_by_date(self, trade_date: date | str) -> list[dict]:
        return self.db.query(
            "SELECT * FROM daily_picks WHERE trade_date=? ORDER BY rank", (_today(trade_date),)
        )

    def latest_date(self) -> str | None:
        row = self.db.query_one("SELECT MAX(trade_date) AS d FROM daily_picks")
        return row["d"] if row and row["d"] else None

    def latest(self) -> list[dict]:
        d = self.latest_date()
        return self.list_by_date(d) if d else []

    def list_since(self, since: date | str) -> list[dict]:
        return self.db.query(
            "SELECT * FROM daily_picks WHERE trade_date >= ? ORDER BY trade_date, rank",
            (_today(since),),
        )


class PlanRepo(BaseRepo):
    def add(self, **kw) -> str:
        pid = kw.pop("id", None) or new_id("pln_")
        now = time.time()
        row = {
            "id": pid,
            "intent_id": kw.get("intent_id"),
            "trade_date": _today(kw.get("trade_date")),
            "symbol": kw["symbol"],
            "side": kw["side"],
            "planned_shares": int(kw.get("planned_shares", 0)),
            "entry_ref_price": kw.get("entry_ref_price"),
            "entry_trigger": kw.get("entry_trigger"),
            "stop_loss_price": kw.get("stop_loss_price"),
            "take_profit": kw.get("take_profit"),
            "max_holding_days": kw.get("max_holding_days"),
            "invalidation_checks": kw.get("invalidation_checks"),
            "status": kw.get("status", "PENDING"),
            "payload": kw.get("payload"),
            "trace_id": kw.get("trace_id"),
            "created_at": now,
            "updated_at": now,
        }
        self.db.insert("plans", row)
        return pid

    def set_status(self, plan_id: str, status: str) -> None:
        self.db.update("plans", {"status": status, "updated_at": time.time()}, "id=?", (plan_id,))

    def list_pending(self, trade_date: date | str) -> list[dict]:
        return self.db.query(
            "SELECT * FROM plans WHERE trade_date=? AND status='PENDING'", (_today(trade_date),)
        )


class SnapshotRepo(BaseRepo):
    def save(self, trade_date: date | str, **kw) -> None:
        self.db.insert(
            "account_snapshots",
            {
                "trade_date": _today(trade_date),
                "total_asset": float(kw["total_asset"]),
                "cash": float(kw["cash"]),
                "market_value": float(kw.get("market_value", 0)),
                "realized_pnl": float(kw.get("realized_pnl", 0)),
                "unrealized_pnl": float(kw.get("unrealized_pnl", 0)),
                "position_count": int(kw.get("position_count", 0)),
                "regime": kw.get("regime"),
                "created_at": time.time(),
            },
            replace=True,
        )

    def history(self, limit: int = 500) -> list[dict]:
        return self.db.query(
            "SELECT * FROM account_snapshots ORDER BY trade_date DESC LIMIT ?", (limit,)
        )[::-1]

    def latest(self) -> dict | None:
        return self.db.query_one("SELECT * FROM account_snapshots ORDER BY trade_date DESC LIMIT 1")


class ExperienceRepo(BaseRepo):
    def add(self, **kw) -> str:
        eid = new_id("exp_")
        self.db.insert(
            "experiences",
            {
                "id": eid,
                "trade_date": _today(kw.get("trade_date")),
                "symbol": kw.get("symbol"),
                "situation": kw["situation"],
                "action": kw.get("action"),
                "outcome": kw.get("outcome"),
                "pnl_pct": kw.get("pnl_pct"),
                "lesson": kw["lesson"],
                "tags": kw.get("tags"),
                "embedding": kw.get("embedding"),
                "created_at": time.time(),
            },
        )
        return eid

    def all(self) -> list[dict]:
        return self.db.query("SELECT * FROM experiences ORDER BY created_at DESC")

    def by_symbol(self, symbol: str, limit: int = 20) -> list[dict]:
        return self.db.query(
            "SELECT * FROM experiences WHERE symbol=? ORDER BY created_at DESC LIMIT ?",
            (symbol, limit),
        )

    def recent(self, since: date | str, *, limit: int = 50,
               tags_like: str | None = None) -> list[dict]:
        """检索某日之后的经验。``tags_like`` 用于按严重度过滤（如 'WARN'）。"""
        sql = "SELECT * FROM experiences WHERE trade_date >= ?"
        args: list = [_today(since)]
        if tags_like:
            sql += " AND tags LIKE ?"
            args.append(f"%{tags_like}%")
        sql += " ORDER BY trade_date DESC, created_at DESC LIMIT ?"
        args.append(int(limit))
        return self.db.query(sql, tuple(args))


class JobRunRepo(BaseRepo):
    """任务执行历史。每次调度落一行，供观察期成功率统计与失败追溯。"""

    def add(self, job_name: str, status: str, *, trade_date: date | str | None = None,
            reason: str = "", elapsed: float = 0.0) -> None:
        self.db.insert(
            "job_runs",
            {
                "job_name": job_name,
                "trade_date": _today(trade_date),
                "status": status,
                "reason": (reason or "")[:500],
                "elapsed": round(float(elapsed or 0.0), 3),
                "started_at": time.time(),
            },
        )

    def by_date(self, trade_date: date | str) -> list[dict]:
        return self.db.query(
            "SELECT * FROM job_runs WHERE trade_date=? ORDER BY started_at",
            (_today(trade_date),),
        )

    def stats(self, since: date | str, until: date | str | None = None) -> list[dict]:
        """按任务统计 OK/FAIL/SKIP 次数与成功率（SKIP 不计入分母）。"""
        sql = ("SELECT job_name,"
               " SUM(status='OK') ok, SUM(status='FAIL') fail, SUM(status='SKIP') skip,"
               " COUNT(*) total"
               " FROM job_runs WHERE trade_date >= ?")
        args: list = [_today(since)]
        if until is not None:
            sql += " AND trade_date <= ?"
            args.append(_today(until))
        sql += " GROUP BY job_name ORDER BY job_name"
        rows = self.db.query(sql, tuple(args))
        for r in rows:
            decided = int(r["ok"] or 0) + int(r["fail"] or 0)
            r["success_rate"] = round(r["ok"] / decided, 4) if decided else None
        return rows

    def prune(self, keep_days: int = 180) -> int:
        cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
        cur = self.db.execute("DELETE FROM job_runs WHERE trade_date < ?", (cutoff,))
        return cur.rowcount


@dataclass
class Repos:
    """仓储集合，方便一次性注入。"""

    db: Database
    orders: OrderRepo
    trades: TradeRepo
    positions: PositionRepo
    risk_events: RiskEventRepo
    llm_calls: LLMCallRepo
    system: SystemStateRepo
    intents: IntentRepo
    picks: PicksRepo
    plans: PlanRepo
    snapshots: SnapshotRepo
    experiences: ExperienceRepo
    job_runs: JobRunRepo

    @classmethod
    def create(cls, db: Database) -> "Repos":
        init_db(db)
        return cls(
            db=db,
            orders=OrderRepo(db),
            trades=TradeRepo(db),
            positions=PositionRepo(db),
            risk_events=RiskEventRepo(db),
            llm_calls=LLMCallRepo(db),
            system=SystemStateRepo(db),
            intents=IntentRepo(db),
            picks=PicksRepo(db),
            plans=PlanRepo(db),
            snapshots=SnapshotRepo(db),
            experiences=ExperienceRepo(db),
            job_runs=JobRunRepo(db),
        )
