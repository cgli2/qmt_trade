"""策略预设中心：把"选股到底用哪套因子配方"变成可切换、可对比、可审计的对象。

设计动机
--------
原系统只有一条写死在 ``features/engine.py`` 里的多因子流水线（按 Regime 切权重）。
用户反馈"策略还是不太行、业绩不佳"——根因之一是**只有一套配方，没有对比、没有按市
况风格切换的候选方案**。本模块把"策略"抽象成一个可命名的 ``StrategyProfile``：

- ``category_weights``：各 Regime 下的因子类别权重（动量/资金/情绪/基本面/质量）。
  这是选股结果差异的头号来源——换权重 = 换一篮子股票。
- ``min_percentile``：各 Regime 下的入选分位门槛（门槛越高 = 越挑剔 = 候选越少越精）。
- ``top_n``：最终候选池规模。
- ``rationale``：这套策略**在什么市况下更可能跑赢**、风险点是什么。让选择有依据，
  而不是拍脑袋。

与现有架构的关系
----------------
不替换 ``DEFAULT_CATEGORY_WEIGHTS``，而是作为**覆盖层（overlay）**：``SelectionPipeline.run
(strategy=...)`` 解析 preset 后，把权重/门槛叠加进 ``FeatureEngine.compute`` 与
``Ranker.rank``。``strategy=None`` 时完全退化为原行为，保证向后兼容。

注意：preset 只动"选哪类因子 + 多挑剔"，不动风控/仓位/执行（那些是另一条独立链路）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..features.regime import Regime

# 五类因子（与 features/base.py 的 CATEGORIES 保持一致）
CATS = ("momentum", "moneyflow", "sentiment", "fundamental", "quality")


@dataclass
class StrategyProfile:
    """一套可命名的选股配方。所有权重按 Regime 键控。"""

    id: str
    name: str
    summary: str
    rationale: str
    #: Regime.value -> {cat: weight}，未列出的 Regime 用 balanced 默认
    category_weights: dict[str, dict[str, float]] = field(default_factory=dict)
    #: Regime.value -> 入选分位门槛（可选覆盖）
    min_percentile: dict[str, float] = field(default_factory=dict)
    #: 候选池规模（可选覆盖）
    top_n: int | None = None
    #: 推荐的运行场景描述（前端展示用）
    best_for: str = ""


def _norm(weights: dict[str, float]) -> dict[str, float]:
    """权重归一化到和=1，缺类补 0。"""
    total = sum(weights.values()) or 1.0
    return {c: round(float(weights.get(c, 0.0)) / total, 4) for c in CATS}


def _w(momentum: float, moneyflow: float, sentiment: float,
        fundamental: float, quality: float) -> dict[str, float]:
    return _norm({
        "momentum": momentum, "moneyflow": moneyflow, "sentiment": sentiment,
        "fundamental": fundamental, "quality": quality,
    })


# ============================================================ 预设定义
# 每套策略对 4 个 Regime 各自给一套权重。为减少样板，先定义每套的"风格向量"，
# 再按 Regime 做轻度缩放（趋势市放大动量/资金，震荡/下跌放大质量/基本面）。
def _scale(base: dict[str, float], *, up: float, down: float) -> dict[str, float]:
    """按 Regime 缩放动量/质量的倾向，不动绝对结构。"""
    out = dict(base)
    out["momentum"] = round(base["momentum"] * up, 4)
    out["moneyflow"] = round(base["moneyflow"] * (1 + (up - 1) * 0.5), 4)
    out["quality"] = round(base["quality"] * down, 4)
    out["fundamental"] = round(base["fundamental"] * down, 4)
    return _norm(out)


# 各策略的"基础风格"（以 RANGE 为基准）
_STYLE = {
    "balanced": _w(0.22, 0.18, 0.10, 0.28, 0.22),
    "momentum_breakout": _w(0.55, 0.22, 0.13, 0.06, 0.04),
    "value_quality": _w(0.15, 0.12, 0.08, 0.30, 0.35),
    "moneyflow_resonance": _w(0.22, 0.46, 0.15, 0.11, 0.06),
    "low_vol_defensive": _w(0.10, 0.12, 0.08, 0.30, 0.40),
}

_PRESET_META = {
    "balanced": dict(
        name="均衡多因子（默认）",
        summary="动量/资金/情绪/基本面/质量五类均衡加权，随 Regime 动态微调。",
        rationale="无明确风格判断时的稳健基线，回撤可控但弹性有限。",
        best_for="震荡市与未知市况下的默认运行。",
    ),
    "momentum_breakout": dict(
        name="动量突破",
        summary="大幅抬升动量（0.55）与资金流权重，追逐中期趋势与突破。",
        rationale="TREND_UP 中动量因子 IC 显著高于其他；但趋势反转时会集中回撤，"
                  "需配合 Gate 止损与 Regime 空仓。",
        best_for="指数站上 20/60 日线、宽度健康的明确上行趋势。",
    ),
    "value_quality": dict(
        name="价值质量",
        summary="重仓基本面（0.30）与质量（0.35），低配动量，做价值+质量防御。",
        rationale="震荡/下行市里高质量低估值标的回撤更小、反弹更稳；"
                  "缺点是错过高弹性题材行情。",
        best_for="震荡或下行市况，或作为组合的对冲底仓。",
    ),
    "moneyflow_resonance": dict(
        name="资金流共振",
        summary="资金流权重拉到 0.46，捕捉主力净流入驱动的阶段行情。",
        rationale="主力资金持续流入是短中期超额收益的高频信号；"
                  "风险是资金流噪声大、易被对倒制造，需基本面兜底。",
        best_for="有结构性增量资金入场的板块轮动期。",
    ),
    "low_vol_defensive": dict(
        name="低波防御",
        summary="质量（0.40）+ 基本面（0.30）主导，极致低波动低回撤取向。",
        rationale="以偿债安全/低下行波动为核心，追求绝对收益而非相对排名；"
                  "在 RISK_OFF 临近时最抗跌，但牛市严重跑输。",
        best_for="市场波动抬升、回撤容忍度低时的防御配置。",
    ),
}


def _build_profiles() -> dict[str, StrategyProfile]:
    out: dict[str, StrategyProfile] = {}
    for sid, style in _STYLE.items():
        meta = _PRESET_META[sid]
        weights: dict[str, dict[str, float]] = {}
        for r in Regime:
            if r is Regime.TREND_UP:
                weights[r.value] = _scale(style, up=1.35, down=0.8)
            elif r is Regime.RANGE:
                weights[r.value] = _scale(style, up=1.0, down=1.0)
            elif r is Regime.TREND_DOWN:
                weights[r.value] = _scale(style, up=0.6, down=1.5)
            else:  # RISK_OFF：基本只配质量/基本面，且实际会空仓
                weights[r.value] = _scale(style, up=0.5, down=1.8)
        # 门槛：越激进的风格在上涨市越敢放宽，越防御的风格越挑剔
        min_pct: dict[str, float] = {}
        if sid in ("momentum_breakout", "moneyflow_resonance"):
            min_pct = {"TREND_UP": 0.45, "RANGE": 0.65, "TREND_DOWN": 0.88, "RISK_OFF": 1.01}
        elif sid in ("value_quality", "low_vol_defensive"):
            min_pct = {"TREND_UP": 0.55, "RANGE": 0.75, "TREND_DOWN": 0.92, "RISK_OFF": 1.01}
        else:
            min_pct = {"TREND_UP": 0.50, "RANGE": 0.70, "TREND_DOWN": 0.90, "RISK_OFF": 1.01}
        out[sid] = StrategyProfile(
            id=sid, name=meta["name"], summary=meta["summary"],
            rationale=meta["rationale"], best_for=meta["best_for"],
            category_weights=weights, min_percentile=min_pct,
            top_n=None if sid == "balanced" else (120 if sid in ("momentum_breakout", "moneyflow_resonance") else 80),
        )
    return out


STRATEGY_PRESETS: dict[str, StrategyProfile] = _build_profiles()
DEFAULT_STRATEGY_ID = "balanced"


def get_strategy_profile(sid: str | None) -> StrategyProfile | None:
    """按 id 取预设；``None``/未知 id 返回 None（调用方据此退化为原行为）。"""
    if not sid:
        return None
    return STRATEGY_PRESETS.get(sid)


def list_strategy_profiles() -> list[dict[str, Any]]:
    """对外暴露的精简清单（不含内部权重细节，前端下拉用）。"""
    return [
        {
            "id": p.id,
            "name": p.name,
            "summary": p.summary,
            "rationale": p.rationale,
            "best_for": p.best_for,
            "default": p.id == DEFAULT_STRATEGY_ID,
        }
        for p in STRATEGY_PRESETS.values()
    ]


def resolve_weights(sid: str | None, regime: Regime) -> dict[str, float] | None:
    """返回某策略在某 Regime 下的因子类别权重（归一化）；无策略返回 None。"""
    p = get_strategy_profile(sid)
    if p is None:
        return None
    return p.category_weights.get(regime.value)


def resolve_min_percentile(sid: str | None, regime: Regime) -> float | None:
    """返回某策略在某 Regime 下的入选分位门槛；无策略返回 None。"""
    p = get_strategy_profile(sid)
    if p is None:
        return None
    return p.min_percentile.get(regime.value)


# ==================================================================== 独立策略
# 与上方「因子预设」体系正交：这些策略**不复用 SelectionPipeline/Regime/因子引擎**，
# 自带选股与执行（见 qmt_trade/strategies/），仅借 CLI/回测外壳。
# 添加新独立策略时在此登记 id 即可，绝不改动现有预设链路。
STANDALONE_STRATEGIES: tuple[str, ...] = (
    "tail_pick", "limit_up", "second_board", "dip_buy", "trend_buy", "etf_t0",
    "stock_t0",
)

_STANDALONE_META: dict[str, dict[str, str]] = {
    "tail_pick": dict(
        name="尾盘选股法 / 一夜持股法",
        summary="尾盘 8 层严格筛选捕捉隔日溢价，T 日 14:30 买入、T+1 开盘 30min 内离场。",
    ),
    "limit_up": dict(
        name="打板策略（周末/节前过滤）",
        summary="昨日涨停 + 连板≥2 + 热门板块前5 + 开盘涨幅2%~8%；周五与节前最后一天不开新仓。",
    ),
    "second_board": dict(
        name="二板龙头战法（陈小群模式）",
        summary="只做连板=2 的二板龙头（不追首板、不接三板以上），热门板块+开盘涨幅2%~8%。",
    ),
    "dip_buy": dict(
        name="尾盘潜伏低吸（改良版）",
        summary="买回调不买上涨：前一日大阳突破趋势 / 主线板块回调3~6%低吸，T 尾盘买入。",
    ),
    "trend_buy": dict(
        name="趋势类买点",
        summary="突破回踩确认 / 均线多头回调 / 上升趋势线回踩，止损+20%/+35% 止盈，持仓数天~数周。",
    ),
    "etf_t0": dict(
        name="ETF T+0 日内回转（底仓做T）",
        summary="ETF 底仓 + 日内 T+0 回转：VWAP 偏离/网格触发高抛低吸，尾盘 T 仓强制归零，"
                "独立于主策略，可单独回测与启停。",
    ),
    "stock_t0": dict(
        name="个股存量持仓做T（高抛低吸）",
        summary="对已持有的个股做日内先卖后买：VWAP 偏离+动量过滤高抛，回落网格/回归低吸，"
                "尾盘 T 仓强制归零，底仓股数不变、成本下降；独立于主策略，可单独回测与启停。",
    ),
}


def is_standalone_strategy(sid: str | None) -> bool:
    """判断是否为独立策略（不入因子预设体系、不走 SelectionPipeline）。"""
    return bool(sid) and sid in STANDALONE_STRATEGIES


def list_standalone_strategies() -> list[dict[str, str]]:
    """对外暴露的独立策略清单（CLI 帮助/UI 用）。"""
    return [{"id": sid, **(_STANDALONE_META.get(sid) or {"name": sid, "summary": ""})}
            for sid in STANDALONE_STRATEGIES]


def build_standalone_backtester(sid: str, settings, hub, *, initial_cash: float = 1_000_000.0,
                                config=None):
    """从统一注册中心构造独立策略回测器。

    ETF T+0 与尾盘短线法和其余独立策略共用同一注册、配置加载及构造入口。
    """
    registry = {
        "tail_pick": ("..strategies.tail_pick", "TailPickBacktester", "TailPickConfig", True),
        "limit_up": ("..strategies.limit_up", "LimitUpBacktester", "LimitUpConfig", False),
        "second_board": ("..strategies.second_board", "SecondBoardBacktester", "SecondBoardConfig", False),
        "dip_buy": ("..strategies.dip_buy", "DipBuyBacktester", "DipBuyConfig", False),
        "trend_buy": ("..strategies.trend_buy", "TrendBuyBacktester", "TrendBuyConfig", False),
        "etf_t0": ("..strategies.etf_t0", "ETFT0Backtester", "ETFT0Config", False),
        "stock_t0": ("..strategies.stock_t0", "StockT0Backtester", "StockT0Config", False),
    }
    try:
        module_name, backtester_name, config_name, tail_pick = registry[sid]
    except KeyError as exc:
        raise KeyError(f"未知独立策略: {sid}") from exc
    from importlib import import_module
    module = import_module(module_name, package=__package__)
    config_cls = getattr(module, config_name)
    if config is None:
        if tail_pick:
            cfg = config_cls.from_settings(settings)
        else:
            from ..strategies.base import load_config
            cfg = load_config(settings, config_cls, sid)
    else:
        cfg = config
    kwargs = dict(initial_cash=initial_cash, config=cfg)
    if tail_pick:
        kwargs["require_minute"] = None
    return getattr(module, backtester_name)(settings, hub, **kwargs)
