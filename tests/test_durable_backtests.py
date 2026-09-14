import json
import multiprocessing
import time
from datetime import date

import pytest

from qmt_trade.storage.db import Database
from qmt_trade.storage.jobs import JobRepository


def test_atomic_results_idempotency_cancel_recovery(tmp_path):
    db = Database(tmp_path / "jobs.duckdb", schema="jobs")
    repo = JobRepository(db)
    job = repo.create("backtest", {"strategy": "trend_buy"}, idempotency_key="double-click")
    assert repo.create("backtest", {"strategy": "trend_buy"}, idempotency_key="double-click")["id"] == job["id"]
    with pytest.raises(ValueError):
        repo.create("backtest", {"strategy": "etf_t0"}, idempotency_key="double-click")
    assert repo.claim(job["id"])
    assert not repo.claim(job["id"])
    with pytest.raises(ValueError):
        repo.finish(job["id"], "succeeded", result={"bad": float("nan")})
    assert repo.get(job["id"])["status"] == "running"
    assert not repo.notifications()
    repo.finish(job["id"], "succeeded", result={"metrics": {"return": .5}, "equity_curve": list(range(3000))})
    assert not repo.finish(job["id"], "succeeded", result={})
    assert len(repo.notifications()) == 1
    assert len(repo.report(job["id"])["equity_curve"]) == 3000
    running = repo.create("backtest")
    repo.claim(running["id"])
    queued = repo.create("backtest")
    cancelled = repo.create("backtest")
    repo.cancel(cancelled["id"])
    assert not repo.claim(cancelled["id"])
    db.close()
    db = Database(tmp_path / "jobs.duckdb", schema="jobs")
    repo = JobRepository(db)
    repo.recover()
    assert repo.get(running["id"])["status"] == "interrupted"
    assert repo.get(queued["id"])["status"] == "queued"
    assert repo.get(cancelled["id"])["status"] == "cancelled"
    repo.recover()
    assert len(repo.notifications()) == 3
    retry = repo.create("backtest", retry_of=running["id"])
    assert retry["attempt"] == 2
    assert retry["retry_of"] == running["id"]
    db.close()


def test_all_parameter_schemas_preserve_defaults_and_advanced():
    from qmt_trade.core.config import get_settings
    from qmt_trade.core.parameter_schema import defaults, schema, validate
    from qmt_trade.core.strategies import STANDALONE_STRATEGIES, STRATEGY_PRESETS
    settings = get_settings()
    for sid in (*STANDALONE_STRATEGIES, *STRATEGY_PRESETS):
        values = defaults(sid, settings)
        assert validate(sid, values, settings) == values
        assert sum(f["level"] == "core" for f in schema(sid, settings)) <= 8
    with pytest.raises(ValueError):
        validate("trend_buy", {"position_fraction": 2}, settings)
    with pytest.raises(ValueError):
        validate("trend_buy", {"max_positions": True}, settings)


def test_strategy_versions_immutable_and_boundary_ack():
    from types import SimpleNamespace
    from qmt_trade.storage.strategies import StrategyRepository
    from qmt_trade.core.config import Settings
    db = Database()
    repo = StrategyRepository(db)
    item = {"id": "one", "strategy_id": "trend_buy", "name": "策略", "enabled": True,
            "created_at": time.time(), "active_version": "v1", "draft": {"params": {"max_positions": 3}},
            "versions": [{"id": "v1", "params": {"max_positions": 3}, "published_at": time.time()}]}
    repo.save([item])
    assert repo.list()[0]["running_version"] is None
    context = SimpleNamespace(settings=Settings({"risk": {"max_loss": .01}}, env_overlay=False))
    repo.apply_at_boundary(context)
    assert repo.list()[0]["running_version"] == "v1"
    assert context.settings.get("strategies.trend_buy.max_positions") == 3
    assert context.settings.get("risk.max_loss") == .01
    item["versions"][0]["params"]["max_positions"] = 9
    with pytest.raises(ValueError):
        repo.save([item])
    assert repo.list()[0]["versions"][0]["params"]["max_positions"] == 3
    db.close()


def test_downsample_keeps_complete_interval_and_extrema():
    from server.routers.backtests import downsample
    data = [{"date": str(i), "equity": float(i)} for i in range(5000)]
    data[3200]["equity"] = -10000
    data[4300]["equity"] = 20000
    sampled = downsample(data, 100)
    assert len(sampled) <= 100
    assert sampled[0] == data[0] and sampled[-1] == data[-1]
    assert data[3200] in sampled and data[4300] in sampled


def test_compute_process_uses_snapshot_and_full_dates(tmp_path):
    from qmt_trade.core.config import Settings
    from qmt_trade.datahub.providers.mock import MockProvider
    from qmt_trade.datahub.manager import DataHub
    from qmt_trade.storage.market import MarketRepository
    from qmt_trade.backtest.snapshot import prepare_snapshot
    from qmt_trade.backtest.worker import compute, file_digest
    settings = Settings({"strategies": {"trend_buy": {"market_filter_enabled": False}}}, env_overlay=False)
    memory = Database()
    hub = DataHub(settings, [MockProvider(n_symbols=5, start="2024-01-02", end="2025-03-01")], store=MarketRepository(memory))
    hub.bars_cache_dir = None
    spec = {"strategy": "trend_buy", "start": "2025-01-06", "end": "2025-01-24", "cash": 1000000}
    snapshot, output = tmp_path / "input.duckdb", tmp_path / "output.duckdb"
    prepare_snapshot(hub, settings, spec, snapshot, lambda s: None, lambda: False)
    memory.close()
    mp = multiprocessing.get_context("spawn")
    receive, send = mp.Pipe(duplex=False)
    stop = mp.Event()
    process = mp.Process(target=compute, args=(spec, settings.as_dict(), str(snapshot), file_digest(snapshot), str(output), send, stop))
    process.start()
    send.close()
    messages = []
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline and (process.is_alive() or receive.poll()):
            if receive.poll(.1):
                try:
                    messages.append(receive.recv())
                except EOFError:
                    break
        process.join(timeout=5)
        assert not process.is_alive(), "worker exceeded test timeout"
        assert process.exitcode == 0
        assert any(m[0] == "result" for m in messages), messages
        db = Database(output)
        result = json.loads(db.scalar("SELECT payload FROM result"))
        assert len(result["equity_dates"]) == len(result["equity_curve"]) > 5
        assert result["strategy"] == "trend_buy"
        assert result["equity_curve"][-1]["date"] >= "2025-01-23"
        assert result["data_hash"] == file_digest(snapshot)
        db.close()
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
        receive.close()
