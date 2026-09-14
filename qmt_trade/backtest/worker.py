"""Compute entrypoint: imports simulation engines only, consumes a closed snapshot."""
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path


def serializable(value):
    if is_dataclass(value):
        return serializable(asdict(value))
    if isinstance(value, dict):
        return {str(k): serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return serializable(value.item())
    return value


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute(spec, settings_data, snapshot_path, snapshot_hash, output_path, channel, stop):
    # Even accidental future context construction cannot open the runtime file.
    os.environ["QMT_RUNTIME_DB"] = str(Path(output_path).with_suffix(".scratch.duckdb"))
    os.environ.pop("QMT_ACCOUNT_ID", None)
    os.environ.pop("QMT_MINI_PATH", None)
    os.environ["QMT_ALLOW_LIVE"] = "0"
    provider = None
    scratch = None
    try:
        if file_digest(snapshot_path) != snapshot_hash:
            raise ValueError("输入快照校验失败")
        from .snapshot import SnapshotProvider
        from ..storage.db import Database
        from ..storage.market import MarketRepository
        from ..core.config import Settings
        from ..core.strategies import STANDALONE_STRATEGIES, build_standalone_backtester
        from ..datahub.manager import DataHub
        from .engine import BacktestEngine
        settings = Settings(settings_data, env_overlay=False)
        provider = SnapshotProvider(snapshot_path)
        from ..storage import industry
        industry._snapshot_mapping = provider.metadata.get("industry_map", {})
        scratch = Database()
        hub = DataHub(settings, [provider], store=MarketRepository(scratch))
        hub.bars_cache_dir = None
        start, end = date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"])
        if spec["strategy"] in STANDALONE_STRATEGIES:
            engine = build_standalone_backtester(spec["strategy"], settings, hub, initial_cash=spec["cash"])
        else:
            from dataclasses import replace
            from ..core.strategies import STRATEGY_PRESETS
            override = settings.section("strategies." + spec["strategy"])
            if override:
                STRATEGY_PRESETS[spec["strategy"]] = replace(STRATEGY_PRESETS[spec["strategy"]], **{k: v for k,v in override.items() if k in {"top_n", "category_weights", "min_percentile"}})
            engine = BacktestEngine(settings, hub, initial_cash=spec["cash"], top_n=spec.get("top_n", 10),
                                    fixed_start=start - timedelta(days=spec.get("warmup", 250)), strategy=spec["strategy"])
        def progress(done, total):
            if stop.is_set():
                raise InterruptedError("任务已取消")
            channel.send(("progress", done, total))
        engine.progress_callback = progress
        result = engine.run(start, end)
        if stop.is_set():
            raise InterruptedError("任务已取消")
        if not result.metrics or not result.equity_curve:
            raise ValueError("; ".join(result.details or ["数据不足或引擎未生成净值"]))
        if spec["strategy"] == "tail_pick" and not getattr(result, "minute_available", False):
            raise ValueError("尾盘策略缺少分钟数据，拒绝将降级结果当作真实回测")
        payload = serializable(result)
        payload.update({"has_metrics": True, "strategy": spec["strategy"], "version_id": spec.get("version_id"),
                        "start": spec["start"], "end": spec["end"], "cash": spec["cash"],
                        "input": spec, "data_hash": snapshot_hash,
                        "data_sources": provider.metadata["sources"], "coverage": provider.metadata["coverage"],
                        "no_trades": not bool(result.trades), "settings_snapshot": settings_data})
        payload["equity_curve"] = [{"date": d, "equity": e} for d, e in zip(payload["equity_dates"], result.equity_curve)]
        if len(payload["equity_curve"]) != len(result.equity_curve):
            raise ValueError("净值日期与数值长度不一致")
        out = Database(output_path)
        try:
            out.execute("CREATE TABLE result (payload VARCHAR)")
            out.execute("INSERT INTO result VALUES (?)", [json.dumps(payload, ensure_ascii=False, allow_nan=False)])
        finally:
            out.close()
        channel.send(("result", file_digest(output_path)))
    except InterruptedError as exc:
        channel.send(("cancelled", str(exc)))
    except BaseException as exc:
        channel.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        if provider:
            provider.close()
        if scratch:
            scratch.close()
        channel.close()
