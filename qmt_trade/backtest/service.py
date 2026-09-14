"""One durable queue and one spawned compute process, owned by the API process."""
from __future__ import annotations

import json
import multiprocessing
import threading
import time
from datetime import date
from pathlib import Path

from ..storage.db import Database
from ..storage.jobs import JobRepository
from ..storage.runtime import runtime_path
from .worker import compute, file_digest


class BacktestService:
    def __init__(self, repository: JobRepository, context_factory, *, work_dir=None):
        self.repo = repository
        self.context_factory = context_factory
        self.work_dir = Path(work_dir or runtime_path().parent / "tasks")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._start_lock = threading.Lock()

    def start(self):
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self.repo.recover()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="backtest-owner", daemon=True)
            self._thread.start()

    def submit(self, spec, settings, *, idempotency_key=None, retry_of=None):
        from ..core.strategies import STANDALONE_STRATEGIES, STRATEGY_PRESETS
        if spec["strategy"] not in {*STANDALONE_STRATEGIES, *STRATEGY_PRESETS}:
            raise ValueError("未知策略，不能执行默认策略代替")
        start, end = date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"])
        if start >= end:
            raise ValueError("开始日期必须早于结束日期")
        if float(spec["cash"]) <= 0:
            raise ValueError("初始资金必须为正数")
        if spec.get("llm"):
            raise ValueError("独立计算进程暂不支持 LLM，请关闭该高级选项")
        if self.repo.db.scalar("SELECT count(*) FROM jobs WHERE kind='backtest' AND status='queued'") >= 32:
            raise ValueError("回测队列已满（32 个），请稍后提交")
        row = self.repo.create("backtest", {"spec": spec, "settings": settings.as_dict()},
                               idempotency_key=idempotency_key, retry_of=retry_of)
        self.start()
        self._wake.set()
        return row

    def retry(self, job_id):
        from ..core.config import Settings
        old = self.repo.get(job_id)
        if not old or old["kind"] != "backtest":
            raise ValueError("回测任务不存在")
        inputs = json.loads(old["input_json"])
        return self.submit(inputs["spec"], Settings(inputs["settings"], env_overlay=False), retry_of=job_id)

    def shutdown(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self):
        while not self._stop.is_set():
            row = self.repo.db.query_one("SELECT * FROM jobs WHERE kind='backtest' AND status='queued' ORDER BY created,id LIMIT 1")
            if not row:
                self._wake.wait(2)
                self._wake.clear()
                continue
            if self.repo.claim(row["id"]):
                try:
                    self._run(row)
                except InterruptedError as exc:
                    self.repo.finish(row["id"], "interrupted" if self._stop.is_set() else "cancelled", error=str(exc))
                except Exception as exc:
                    self.repo.finish(row["id"], "failed", error=f"{type(exc).__name__}: {exc}", error_code="BACKTEST_FAILED")

    def _run(self, row):
        from ..core.config import Settings
        from .snapshot import prepare_snapshot
        job_id = row["id"]
        inputs = json.loads(row["input_json"])
        spec, settings = inputs["spec"], Settings(inputs["settings"], env_overlay=False)
        c = self.context_factory()
        if "mock" in getattr(c.hub, "providers", {}):
            raise ValueError("正式回测拒绝 MockProvider 合成行情")
        def cancelled():
            return self._stop.is_set() or self.repo.get(job_id)["cancel_requested"]
        snapshot = self.work_dir / (job_id + ".input.duckdb")
        output = self.work_dir / (job_id + ".result.duckdb")
        self.repo.progress(job_id, "检查数据 / 准备快照")
        prepare_snapshot(c.hub, settings, spec, snapshot,
                         lambda stage: self.repo.progress(job_id, stage), cancelled)
        if cancelled():
            raise InterruptedError("准备数据后确认取消")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        stop = context.Event()
        process = context.Process(target=compute, args=(spec, settings.as_dict(), str(snapshot), file_digest(snapshot), str(output), child, stop), daemon=True)
        process.start()
        child.close()
        self.repo.progress(job_id, "计算")
        result_hash = None
        failure = None
        started, cancel_at, heartbeat = time.monotonic(), None, 0.0
        try:
            while process.is_alive() or parent.poll():
                now = time.monotonic()
                if cancelled() or now - started > 3600:
                    stop.set()
                    cancel_at = cancel_at or now
                    if now - cancel_at > 5 and process.is_alive():
                        process.terminate()
                if parent.poll(0.2):
                    try:
                        event = parent.recv()
                    except EOFError:
                        break
                    if event[0] == "progress":
                        self.repo.progress(job_id, "计算", event[1], event[2])
                    elif event[0] == "result":
                        result_hash = event[1]
                    else:
                        failure = event
                if now - heartbeat >= 5:
                    self.repo.db.update("jobs", {"heartbeat": time.time()}, "id=?", [job_id])
                    heartbeat = now
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join()
            if cancelled():
                raise InterruptedError("计算进程已停止")
            if cancel_at:
                raise TimeoutError("计算超过一小时预算，进程已停止")
            if failure or process.exitcode != 0 or not result_hash:
                raise RuntimeError(str(failure or f"计算进程异常退出：{process.exitcode}"))
            if file_digest(output) != result_hash:
                raise ValueError("结果库摘要校验失败")
            self.repo.progress(job_id, "生成报告")
            result_db = Database(output)
            try:
                result = json.loads(result_db.scalar("SELECT payload FROM result"))
            finally:
                result_db.close()
            if result.get("input") != spec:
                raise ValueError("结果输入与任务提交快照不一致")
            self.repo.finish(job_id, "succeeded", result=result)
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
            parent.close()
