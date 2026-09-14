"""Durable jobs, reports and transactional notification outbox."""
import json
import time
import uuid

from .db import Database

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


class JobRepository:
    def __init__(self, db: Database):
        self.db = db
        db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id VARCHAR PRIMARY KEY, kind VARCHAR NOT NULL, status VARCHAR NOT NULL,
            stage VARCHAR, created DOUBLE NOT NULL, started DOUBLE, finished DOUBLE,
            heartbeat DOUBLE, cancel_requested BOOLEAN DEFAULT false,
            completed BIGINT DEFAULT 0, total BIGINT, attempt INTEGER DEFAULT 1,
            retry_of VARCHAR, input_json VARCHAR NOT NULL, input_hash VARCHAR,
            idempotency_key VARCHAR UNIQUE, error VARCHAR, error_code VARCHAR, report_id VARCHAR);
        CREATE TABLE IF NOT EXISTS events (id VARCHAR PRIMARY KEY, job_id VARCHAR, stage VARCHAR, created DOUBLE);
        CREATE TABLE IF NOT EXISTS reports (id VARCHAR PRIMARY KEY, job_id VARCHAR UNIQUE, created DOUBLE, payload VARCHAR NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications (id VARCHAR PRIMARY KEY, job_id VARCHAR, status VARCHAR, created DOUBLE, read_at DOUBLE);
        """)
        from .backtests import BacktestRepository
        self.backtests = BacktestRepository(db)

    def create(self, kind, inputs=None, *, idempotency_key=None, retry_of=None):
        import hashlib
        payload = json.dumps(inputs or {}, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.db.transaction():
            if idempotency_key:
                old = self.db.query_one("SELECT * FROM jobs WHERE idempotency_key=?", [idempotency_key])
                if old:
                    if old["input_hash"] != digest or old["kind"] != kind:
                        raise ValueError("幂等请求键已绑定其他输入")
                    return old
            row = {"id": uuid.uuid4().hex, "kind": kind, "status": "queued", "stage": "排队",
                   "created": time.time(), "input_json": payload, "input_hash": digest,
                   "idempotency_key": idempotency_key, "retry_of": retry_of}
            if retry_of:
                previous = self.get(retry_of)
                if previous is None or previous["status"] not in {"failed", "interrupted", "cancelled"}:
                    raise ValueError("只能重跑失败、中断或取消的任务")
                row["attempt"] = previous["attempt"] + 1
            self.db.insert("jobs", row)
        return self.get(row["id"])

    def get(self, job_id):
        return self.db.query_one("SELECT * FROM jobs WHERE id=?", [job_id])

    def list(self, limit=20, offset=0, kind=None):
        where, args = ("WHERE kind=?", [kind]) if kind else ("", [])
        return self.db.query(f"SELECT * FROM jobs {where} ORDER BY created DESC,id DESC LIMIT ? OFFSET ?", args + [max(1, min(100, limit)), max(0, offset)])

    def claim(self, job_id):
        with self.db.transaction():
            return self.db.update("jobs", {"status": "running", "started": time.time(), "heartbeat": time.time()},
                                  "id=? AND status='queued' AND NOT cancel_requested", [job_id]) == 1

    def progress(self, job_id, stage, completed=None, total=None):
        fields = {"stage": stage, "heartbeat": time.time()}
        if completed is not None:
            fields["completed"] = completed
        if total is not None:
            fields["total"] = total
        with self.db.transaction():
            if self.db.update("jobs", fields, "id=? AND status='running'", [job_id]):
                self.db.insert("events", {"id": uuid.uuid4().hex, "job_id": job_id, "stage": stage, "created": time.time()})

    def finish(self, job_id, status, *, result=None, error=None, error_code=None):
        if status not in TERMINAL:
            raise ValueError("Invalid terminal status")
        with self.db.transaction():
            old = self.get(job_id)
            if not old or old["status"] in TERMINAL:
                return False
            report_id = None
            if status == "succeeded":
                if result is None:
                    raise ValueError("成功任务必须先保存结果")
                report_id = job_id
                payload = json.dumps(result, ensure_ascii=False, default=str, allow_nan=False)
                if old["kind"] == "backtest" and result.get("strategy") and result.get("equity_curve"):
                    self.backtests.save(report_id, result)
                    payload = json.dumps({"backtest_run_id": report_id})
                self.db.insert("reports", {"id": report_id, "job_id": job_id, "created": time.time(), "payload": payload})
            self.db.update("jobs", {"status": status, "finished": time.time(), "report_id": report_id,
                                    "error": error, "error_code": error_code}, "id=?", [job_id])
            self.db.insert_ignore("notifications", {"id": job_id + ":terminal", "job_id": job_id,
                                                      "status": status, "created": time.time()})
        return True

    def cancel(self, job_id):
        with self.db.transaction():
            row = self.get(job_id)
            if row and row["status"] not in TERMINAL:
                self.db.update("jobs", {"cancel_requested": True}, "id=?", [job_id])
                if row["status"] == "queued":
                    self.finish(job_id, "cancelled")
        return self.get(job_id)

    def recover(self):
        # Called once by the exclusive owner at startup, never by list/read.
        for row in self.db.query("SELECT id FROM jobs WHERE status='running' OR (status='queued' AND kind<>'backtest')"):
            self.finish(row["id"], "interrupted", error="服务退出，计算进程状态不可恢复，请重跑", error_code="OWNER_RESTART")

    def report(self, report_id):
        row = self.db.query_one("SELECT payload FROM reports WHERE id=?", [report_id])
        if not row:
            return None
        result = json.loads(row["payload"])
        if result.get("backtest_run_id"):
            return self.backtests.summary(result["backtest_run_id"])
        return result

    def notifications(self, limit=50):
        return self.db.query("SELECT * FROM notifications ORDER BY created DESC,id DESC LIMIT ?", [max(1, min(100, limit))])

    def mark_read(self, notification_id):
        return self.db.update("notifications", {"read_at": time.time()}, "id=? AND read_at IS NULL", [notification_id])
