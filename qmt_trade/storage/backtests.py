"""Queryable complete backtest results with explicit metric units."""
import json

FRACTIONS = {"total_return", "annual_return", "cagr", "max_drawdown", "win_rate", "volatility", "turnover", "gross_return", "net_return"}
COUNTS = {"trade_count", "n_trades", "n_days", "closed_trade_count"}


class BacktestRepository:
    def __init__(self, db):
        self.db = db
        db.executescript("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
          id VARCHAR PRIMARY KEY, strategy VARCHAR, version_id VARCHAR, start_date DATE, end_date DATE,
          cash DOUBLE, data_hash VARCHAR, code_version VARCHAR, metadata VARCHAR);
        CREATE TABLE IF NOT EXISTS backtest_metrics (run_id VARCHAR, name VARCHAR, value_json VARCHAR, unit VARCHAR, PRIMARY KEY(run_id,name));
        CREATE TABLE IF NOT EXISTS backtest_equity (run_id VARCHAR, seq BIGINT, date VARCHAR, equity DOUBLE, PRIMARY KEY(run_id,seq));
        CREATE TABLE IF NOT EXISTS backtest_trades (run_id VARCHAR, seq BIGINT, symbol VARCHAR, trade_time VARCHAR, payload VARCHAR, PRIMARY KEY(run_id,seq));
        """)

    def save(self, run_id, result):
        if self.db.scalar("SELECT count(*) FROM backtest_runs WHERE id=?", [run_id]):
            raise ValueError("报告已导入")
        metadata = {k: v for k, v in result.items() if k not in {"metrics", "equity_curve", "equity_dates", "trades"}}
        self.db.insert("backtest_runs", {"id": run_id, "strategy": result.get("strategy"), "version_id": result.get("version_id"),
                       "start_date": result.get("start"), "end_date": result.get("end"), "cash": result.get("cash"),
                       "data_hash": result.get("data_hash"), "code_version": result.get("code_version"), "metadata": json.dumps(metadata, ensure_ascii=False)})
        self.db.executemany("INSERT INTO backtest_metrics VALUES (?,?,?,?)", [(run_id, k, json.dumps(v, ensure_ascii=False),
                            "fraction" if k in FRACTIONS else "count" if k in COUNTS else "ratio" if k in {"sharpe", "calmar", "profit_factor"} else "value") for k, v in result.get("metrics", {}).items()])
        self.db.executemany("INSERT INTO backtest_equity VALUES (?,?,?,?)", [(run_id, i, p["date"], p["equity"]) for i, p in enumerate(result["equity_curve"])])
        self.db.executemany("INSERT INTO backtest_trades VALUES (?,?,?,?,?)", [(run_id, i, p.get("symbol"), str(p.get("time") or p.get("filled_at") or p.get("date") or ""), json.dumps(p, ensure_ascii=False)) for i, p in enumerate(result.get("trades", []))])

    def summary(self, run_id):
        row = self.db.query_one("SELECT metadata FROM backtest_runs WHERE id=?", [run_id])
        if not row:
            return None
        result = json.loads(row["metadata"])
        metrics = self.db.query("SELECT name,value_json,unit FROM backtest_metrics WHERE run_id=? ORDER BY name", [run_id])
        result["metrics"] = {r["name"]: json.loads(r["value_json"]) for r in metrics}
        result["metric_units"] = {r["name"]: r["unit"] for r in metrics}
        result["equity_curve"] = self.db.query("SELECT date,equity FROM backtest_equity WHERE run_id=? ORDER BY seq", [run_id])
        result["trade_count"] = self.db.scalar("SELECT count(*) FROM backtest_trades WHERE run_id=?", [run_id])
        return result

    def trades(self, run_id, offset=0, limit=50):
        return [json.loads(r["payload"]) for r in self.db.query("SELECT payload FROM backtest_trades WHERE run_id=? ORDER BY seq LIMIT ? OFFSET ?", [run_id, max(1, min(limit, 100)), max(0, offset)])]
