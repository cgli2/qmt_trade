"""Form metadata derived from the actual strategy configuration classes."""
from dataclasses import asdict
import math

CORE = ("symbols", "pattern", "max_positions", "position_fraction", "base_fraction",
        "t_slice_ratio", "stop_pct", "stop_floor_pct", "overnight_stop_pct",
        "max_hold_days", "max_trades_per_symbol_per_day")
LABELS = {"symbols": "交易标的", "pattern": "买点形态", "max_positions": "最大持仓数",
          "position_fraction": "单仓资金比例", "base_fraction": "底仓比例", "t_slice_ratio": "做 T 比例",
          "stop_pct": "止损比例", "stop_floor_pct": "止损底线", "overnight_stop_pct": "隔夜止损",
          "max_hold_days": "最大持有天数", "max_trades_per_symbol_per_day": "每标的每日交易上限"}


def defaults(sid, settings):
    from .strategies import STANDALONE_STRATEGIES, STRATEGY_PRESETS
    if sid in STANDALONE_STRATEGIES:
        from importlib import import_module
        names = {"tail_pick": "TailPickConfig", "limit_up": "LimitUpConfig", "second_board": "SecondBoardConfig",
                 "dip_buy": "DipBuyConfig", "trend_buy": "TrendBuyConfig", "etf_t0": "ETFT0Config", "stock_t0": "StockT0Config"}
        cls = getattr(import_module("qmt_trade.strategies." + sid), names[sid])
        if sid == "tail_pick":
            config = cls.from_settings(settings)
        else:
            from ..strategies.base import load_config
            config = load_config(settings, cls, sid)
        return asdict(config)
    if sid in STRATEGY_PRESETS:
        profile = STRATEGY_PRESETS[sid]
        return {"top_n": profile.top_n or 100, "category_weights": profile.category_weights,
                "min_percentile": profile.min_percentile}
    raise ValueError("未知可配置策略")


def schema(sid, settings):
    values = defaults(sid, settings)
    core = [k for k in CORE if k in values][:8]
    fields = []
    for key, value in values.items():
        kind = "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "string"
        fields.append({"key": key, "label": LABELS.get(key, key), "type": kind, "default": value,
                       "level": "core" if key in core else "advanced", "group": "核心参数" if key in core else "高级参数",
                       "unit": "fraction" if key in {"position_fraction", "base_fraction", "t_slice_ratio", "stop_pct", "stop_floor_pct", "overnight_stop_pct"} else None,
                       "description": "比例使用小数，例如 0.07 表示 7%" if key.endswith(("_pct", "_fraction", "_ratio")) else "沿用策略现有参数定义"})
    return fields


def validate(sid, params, settings):
    from .config import _deep_merge
    base = defaults(sid, settings)
    unknown = set(params) - set(base)
    if unknown:
        raise ValueError(f"不支持的参数：{', '.join(sorted(unknown))}")
    merged = _deep_merge(base, params)
    for field in schema(sid, settings):
        key, kind = field["key"], field["type"]
        value = merged[key]
        types = {"boolean": bool, "integer": int, "number": (int, float), "array": list, "object": dict, "string": str}
        if base[key] is not None and (not isinstance(value, types[kind]) or kind in {"integer", "number"} and isinstance(value, bool)):
            raise ValueError(f"{key} 类型错误")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
            raise ValueError(f"{key} 必须是有限数")
        if field["unit"] == "fraction" and not 0 <= value <= 1:
            raise ValueError(f"{key} 必须在 0 到 1 之间")
        if key in {"max_positions", "max_hold_days", "max_trades_per_symbol_per_day", "top_n"} and value <= 0:
            raise ValueError(f"{key} 必须大于 0")
        if key == "symbols" and any(not isinstance(v, str) or not v.strip() for v in value):
            raise ValueError("标的列表必须是非空字符串")
    return merged
