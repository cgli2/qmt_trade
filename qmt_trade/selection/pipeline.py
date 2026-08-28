"""L2-a 选股漏斗编排（设计 6.3）。

把 Regime 判定 → L0 硬过滤 → 因子打分 → L1 排序 串成一条可复现的流水线。

**这是回测与实盘唯一共用的选股入口（P7）。** 回测里滚动调用同一个
``SelectionPipeline.run(asof=...)``，不允许另写一份"回测专用"的选股逻辑 ——
那是策略回测最经典的自欺方式：回测代码和实盘代码悄悄分叉，
回测跑出年化 60%，实盘一上就亏，还查不出差在哪。

流水线本身**不含 LLM**（P5：先跑通无 LLM 的确定性闭环）。
LLM 深度研判是下一级（6.4），消费本模块产出的 ``CandidateSet``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from ..core.logging import get_logger
from ..core.strategies import (resolve_min_percentile, resolve_weights,
                               get_strategy_profile)
from ..datahub.pit import as_of_pre_open
from ..features.engine import FeatureEngine, FeatureResult
from ..features.regime import Regime, RegimeDetector, RegimeSnapshot
from .ranker import RankResult, Ranker
from .screener import ScreenResult, Screener

logger = get_logger("selection.pipeline")

#: Phase 2（2026-08-12）：市况 → 防御策略预设 的映射（auto_defensive_strategy 用）。
#: 下跌市/风险期用价值质量、低波防御配方，替代均衡/动量（V0~V4 真实数据确认的亏钱主因）。
_DEFENSIVE_BY_REGIME = {
    Regime.TREND_DOWN: "value_quality",
    Regime.RISK_OFF: "low_vol_defensive",
}


@dataclass
class CandidateSet:
    """选股漏斗的最终产出。下游（LLM 研判 / 组合构建 / 回测）的统一输入。"""

    asof: date
    regime: RegimeSnapshot
    symbols: list[str]
    #: 入选明细（symbol/score/industry/rank + 各因子分位）
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    screen: ScreenResult | None = None
    ranking: RankResult | None = None
    features: FeatureResult | None = None
    timings: dict[str, float] = field(default_factory=dict)
    #: 数据不足/异常导致的降级说明，非空时下游应更保守
    degraded: list[str] = field(default_factory=list)
    #: 本次选股使用的策略预设 id（None = 默认均衡多因子）
    strategy: str | None = None

    @property
    def n(self) -> int:
        return len(self.symbols)

    @property
    def is_empty(self) -> bool:
        return not self.symbols

    def shortlist(self, n: int) -> list[str]:
        """取前 n 只送 LLM。"""
        return self.symbols[:n]

    def report(self) -> str:
        r = self.regime
        lines = [
            f"{'=' * 60}",
            f"选股结果 asof={self.asof}  Regime={r.regime.value}"
            f"（仓位上限 {r.max_position:.0%}，分位门槛≥{r.min_percentile:.2f}"
            + (f"/绝对分≥{r.min_score:.2f}" if r.min_score > 0 else "")
            + "）",
            f"  {r.reason}",
            f"{'=' * 60}",
        ]
        if self.screen:
            lines.append(self.screen.funnel_report())
        if self.ranking:
            lines.append(self.ranking.report())
        if self.degraded:
            lines.append("  ⚠ 降级: " + "; ".join(self.degraded))
        lines.append(
            "  耗时: " + ", ".join(f"{k}={v:.2f}s" for k, v in self.timings.items())
        )
        if self.n:
            head = self.frame.head(10)
            lines.append("  Top10:")
            for _, row in head.iterrows():
                lines.append(
                    f"    {int(row['rank']):>3}. {row['symbol']}  "
                    f"score={row['score']:.4f}  {row.get('industry', '')}"
                )
        else:
            lines.append("  候选池为空")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "asof": str(self.asof),
            "regime": self.regime.to_dict() if self.regime else None,
            "n": self.n,
            "symbols": list(self.symbols),
            "screen": self.screen.to_dict() if self.screen else None,
            "ranking": self.ranking.to_dict() if self.ranking else None,
            "timings": dict(self.timings),
            "degraded": list(self.degraded),
            "strategy": self.strategy,
        }


class SelectionPipeline:
    def __init__(self, settings, hub):
        self.settings = settings
        self.hub = hub
        self.screener = Screener(settings)
        self.ranker = Ranker(settings)
        self.engine = FeatureEngine(settings, hub)
        self.detector = RegimeDetector(settings, hub)
        cfg = settings.section("selection") or {}
        self.llm_shortlist = int(cfg.get("llm_shortlist", 20))
        #: RISK_OFF 时是否直接返回空池。默认 True —— 空仓也是一种仓位，
        #: 而且是唯一在系统性风险里稳赚不赔的仓位。
        self.skip_on_risk_off = bool(cfg.get("skip_on_risk_off", True))
        #: Phase 2（2026-08-12）：未显式指定策略预设时，按市况自动切换防御预设。
        #: TREND_DOWN → value_quality（价值质量防御）；RISK_OFF → low_vol_defensive。
        #: 修复 V0~V4 真实数据确认的问题：下跌市仍用均衡/动量配方 → 满仓接飞刀。
        #: 显式传入 strategy 时该机制不生效（显式优先）。
        self.auto_defensive = bool(cfg.get("auto_defensive_strategy", True))

    # ------------------------------------------------------------------ 主流程
    def run(
        self,
        asof: date | datetime,
        *,
        universe: list[str] | None = None,
        history_start: date | None = None,
        top_n: int | None = None,
        extra_symbols: list[str] | None = None,
        ic_overrides: dict[str, float] | None = None,
        strategy: str | None = None,
    ) -> CandidateSet:
        """执行一次完整选股。

        :param asof: 决策时点。传 ``date`` 时自动按**盘前 09:00** 处理。
        :param universe: 候选全集，None 表示取全市场。
        :param history_start: 固定历史起点。回测中务必传固定值，
                              否则滚动窗口起点漂移会让 rolling 类因子出现
                              浮点级别的不可复现差异（P6 血泪教训）。
        :param extra_symbols: 滚动观察池标的。即使当日未达 Regime 门槛/
                              被行业配额挤出，只要仍通过 L0 硬过滤且有因子分，
                              就并入候选池头部送 LLM 续研（去留由研判决定）。
        :param ic_overrides: L5 复盘回灌的因子权重覆盖（因子名 → 权重），
                             用于对持续负 IC 的因子降权。回测/实盘同一入口（P7）。
        """
        day = asof.date() if isinstance(asof, datetime) else asof
        cut = asof if isinstance(asof, datetime) else as_of_pre_open(day)
        timings: dict[str, float] = {}
        degraded: list[str] = []
        # 把固定历史起点下传给 Regime 检测器：指数历史一次性拉全量并本地切片，
        # 避免每日滑动起点重复下载沪深300 / 创业板指。
        if history_start is not None:
            self.detector.fixed_start = history_start

        # ---- 0. 全集
        t = time.perf_counter()
        syms = list(universe) if universe else self._all_symbols(day)
        instruments = self._instrument_map(syms, day)
        timings["universe"] = time.perf_counter() - t
        if not syms:
            logger.error("候选全集为空 asof=%s", day)
            return CandidateSet(asof=day, regime=self._fallback_regime(day),
                                symbols=[], degraded=["候选全集为空"], timings=timings)

        # ---- 1. Regime
        t = time.perf_counter()
        regime = self.detector.detect(cut)
        timings["regime"] = time.perf_counter() - t
        if regime.degraded:
            degraded.append("Regime 判定降级（指数数据不足）")

        if regime.regime is Regime.RISK_OFF and self.skip_on_risk_off:
            logger.warning("RISK_OFF：跳过选股，返回空候选池 asof=%s", day)
            return CandidateSet(asof=day, regime=regime, symbols=[],
                                degraded=degraded + ["RISK_OFF 空仓"], timings=timings,
                                strategy=strategy)

        # ---- 策略预设覆盖（overlay）：解析因子权重 / 入选门槛 / 候选规模 ----
        # Phase 2：未显式指定时按市况自动切防御预设（显式 strategy 优先）。
        if strategy is None and self.auto_defensive:
            _auto = _DEFENSIVE_BY_REGIME.get(regime.regime)
            if _auto is not None:
                strategy = _auto
                logger.info("市况 %s → 自动切换防御预设 %s", regime.regime.value, _auto)
        profile = get_strategy_profile(strategy)
        cat_weights_override = resolve_weights(strategy, regime.regime)
        min_pct_override = resolve_min_percentile(strategy, regime.regime)
        rank_top_n = top_n
        if profile and profile.top_n:
            rank_top_n = profile.top_n
        if profile:
            logger.info("应用策略预设 %s（Regime=%s，权重覆盖=%s，门槛=%.2f）",
                        profile.id, regime.regime.value,
                        "是" if cat_weights_override else "否",
                        min_pct_override if min_pct_override is not None
                        else regime.min_percentile)

        # ---- 2. 取数（一次取全，后面 L0/因子共用同一份 panel，避免重复 IO）
        t = time.perf_counter()
        panel = self.engine.build_panel(syms, cut, start=history_start)
        timings["panel"] = time.perf_counter() - t
        if panel is None or panel.empty:
            logger.error("行情面板为空 asof=%s", day)
            return CandidateSet(asof=day, regime=regime, symbols=[],
                                degraded=degraded + ["行情面板为空"], timings=timings,
                                strategy=strategy)

        # ---- 3. L0 硬过滤
        t = time.perf_counter()
        screen = self.screener.screen(panel, asof=day, instruments=instruments)
        timings["screen"] = time.perf_counter() - t
        if not screen.passed:
            logger.error("L0 硬过滤后无标的存活 asof=%s\n%s", day, screen.funnel_report())
            return CandidateSet(asof=day, regime=regime, symbols=[], screen=screen,
                                degraded=degraded + ["硬过滤后候选为空"], timings=timings,
                                strategy=strategy)

        # ---- 4. 因子打分（只算存活标的，省掉一大半计算量）
        t = time.perf_counter()
        sub = panel[panel["symbol"].isin(set(screen.passed))]
        feats = self.engine.compute(screen.passed, cut, regime=regime, panel=sub,
                                    ic_overrides=ic_overrides,
                                    cat_weights_override=cat_weights_override)
        timings["factors"] = time.perf_counter() - t
        if feats.frame is None or feats.frame.empty:
            return CandidateSet(asof=day, regime=regime, symbols=[], screen=screen,
                                degraded=degraded + ["因子打分为空"], timings=timings,
                                strategy=strategy)

        # ---- 5. L1 排序 + 行业分散
        t = time.perf_counter()
        scored = feats.frame
        if "industry" not in scored.columns:
            scored = scored.copy()
            scored["industry"] = scored["symbol"].map(
                {s: getattr(i, "industry", "") for s, i in instruments.items()}
            )
        ranking = self.ranker.rank(
            scored, asof=day, top_n=rank_top_n,
            min_score=regime.min_score,
            min_percentile=(min_pct_override if min_pct_override is not None
                            else regime.min_percentile),
            universe=screen.passed,
        )
        timings["rank"] = time.perf_counter() - t
        timings["total"] = sum(timings.values())

        if ranking.n == 0:
            degraded.append(
                f"无标的达到 Regime 门槛（分位≥{regime.min_percentile:.2f}"
                + (f"/绝对分≥{regime.min_score:.2f}" if regime.min_score > 0 else "")
                + "）"
            )

        # ---- 6. 滚动观察池并入：头部插入，保证进入 LLM shortlist 续研
        if extra_symbols:
            t = time.perf_counter()
            ranking, injected = self._merge_extra(
                ranking, feats.frame, screen.passed, panel, instruments, extra_symbols)
            timings["rolling"] = time.perf_counter() - t
            if injected:
                degraded.append(f"滚动观察池并入 {injected} 只（续研候选）")

        # ---- 7. frame 的 close 列替换为不复权真实价
        # 下游（研判 ref_price → plan 参考价 → 下单/止损）必须真实价。
        ranking_frame = self._raw_close_overlay(ranking.frame, day)

        return CandidateSet(
            asof=day, regime=regime, symbols=ranking.selected, frame=ranking_frame,
            screen=screen, ranking=ranking, features=feats,
            timings=timings, degraded=degraded, strategy=strategy,
        )

    # ------------------------------------------------------------------ 辅助
    def _raw_close_overlay(self, frame: pd.DataFrame, day: date) -> pd.DataFrame:
        """把 frame 的 close 列换成**不复权**收盘价。

        因子面板必须后复权（保证时序可比），但候选池 frame 的 close 会流进
        研判 ref_price → 计划参考价 → 下单/止损，必须是真实价 —— 复权价
        报出去就是拿假钱真下单。失败时保留原列（下游执行层还有一道
        价空间纠偏兜底），不阻断选股主流程。
        """
        if frame is None or frame.empty or "symbol" not in frame.columns:
            return frame
        try:
            from datetime import timedelta

            from ..datahub.types import Adjust, Freq
            raw = self.hub.get_bars(list(frame["symbol"]), Freq.D1,
                                    day - timedelta(days=10), day, Adjust.NONE)
        except Exception as exc:                    # noqa: BLE001
            logger.warning("真实价替换 close 取数失败: %s", exc)
            return frame
        if raw is None or raw.empty:
            return frame
        closes = (raw.sort_values("date").groupby("symbol")["close"].last()
                  if "date" in raw.columns else raw.groupby("symbol")["close"].last())
        out = frame.copy()
        mapped = out["symbol"].map(closes)
        out["close"] = mapped.where(mapped > 0, out.get("close", mapped))
        return out

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def _merge_extra(ranking: RankResult, feats_frame: pd.DataFrame,
                     passed: list[str], panel: pd.DataFrame,
                     instruments: dict, extra: list[str]) -> tuple[RankResult, int]:
        """把滚动观察池标的并入 L1 排序结果。

        只保留仍通过 L0 硬过滤且有因子分的（连硬过滤都过不了的必须出局，
        这是滚动机制里唯一不可商量的底线）。新增标的插在候选头部，
        保证不被 ``shortlist(n)`` 截掉；``rank`` 列按新顺序重赋。
        返回 ``(ranking, 新增数量)``。
        """
        passed_set = set(passed)
        dropped = [s for s in extra if s not in passed_set]
        have = set(ranking.selected)
        add = [s for s in extra if s in passed_set and s not in have]
        if not add and not dropped:
            return ranking, 0
        if feats_frame is None or feats_frame.empty:
            return ranking, 0

        rows = feats_frame[feats_frame["symbol"].isin(add)]
        found = list(rows["symbol"])
        if not found:
            return ranking, 0
        if "industry" not in rows.columns:
            rows = rows.copy()
            rows["industry"] = rows["symbol"].map(
                {s: getattr(i, "industry", "") for s, i in (instruments or {}).items()})
        if "close" not in rows.columns and panel is not None and not panel.empty:
            closes = (panel.sort_values("date").groupby("symbol")["close"].last()
                      if "date" in panel.columns else panel.groupby("symbol")["close"].last())
            rows = rows.copy()
            rows["close"] = rows["symbol"].map(closes).fillna(0.0)

        base = ranking.frame.drop(columns="rank", errors="ignore")
        merged = pd.concat([rows, base], ignore_index=True)
        merged["rank"] = range(1, len(merged) + 1)
        ranking.frame = merged
        ranking.selected = found + list(ranking.selected)
        ranking.stats["rolling_injected"] = len(found)
        if dropped:
            logger.info("滚动候选未过硬过滤被剔除 asof=%s: %s", ranking.asof, ",".join(dropped))
        return ranking, len(found)

    def _all_symbols(self, day: date) -> list[str]:
        try:
            infos = self.hub.get_instruments()
        except Exception as exc:  # noqa: BLE001 - 取全集失败必须降级而非崩溃
            logger.error("获取全市场标的失败: %s", exc)
            return []
        if isinstance(infos, dict):
            return list(infos.keys())
        return [getattr(i, "symbol", str(i)) for i in (infos or [])]

    def _instrument_map(self, syms: list[str], day: date) -> dict:
        """symbol → InstrumentInfo。

        TODO(PIT)：``get_instruments`` 目前只给"当前"的 ST/上市日/市值，
        严格来说 is_st 是随时间变化的状态（某天被 ST、某天摘帽）。
        回测跨越较长区间时会有轻微前视偏差 —— 用今天的 ST 名单去过滤两年前的截面，
        等于提前知道了哪些票后来出事。接入真实数据源时需要改成按日期取快照。
        当前 mock/akshare 都只提供最新快照，先记账。
        """
        try:
            infos = self.hub.get_instruments(syms)
        except Exception as exc:  # noqa: BLE001
            logger.error("获取标的信息失败，硬过滤将退化为仅行情规则: %s", exc)
            return {}
        if isinstance(infos, dict):
            return infos
        return {getattr(i, "symbol", ""): i for i in (infos or []) if getattr(i, "symbol", "")}

    def _fallback_regime(self, day: date) -> RegimeSnapshot:
        return RegimeSnapshot(
            asof=day, regime=Regime.RISK_OFF, max_position=0.0, min_score=1.0,
            reason="无法构造候选全集，保守置为 RISK_OFF", degraded=True,
        )
