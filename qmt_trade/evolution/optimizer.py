"""Walk-forward 参数寻优（设计 L5）。

**为什么必须 walk-forward 而不是全样本网格搜索**：全样本最优参数几乎必然过拟合，
在样本外一文不值。这里用滚动的「训练窗 → 立即在紧邻的验证窗上检验」，
只有在**多个不重叠验证窗上都稳定有效**的参数才会被采纳。

三道防过拟合闸门：
1. **样本外为准**：排序只看验证窗（OOS）指标，训练窗指标仅供参考；
2. **稳定性惩罚**：各验证窗表现的标准差越大，得分扣得越狠；
3. **参数变更幅度限制**：单次调参不得超过 ``max_step``，避免策略被一次拟合带跑偏。
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Sequence

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class Window:
    train_start: date
    train_end: date
    valid_start: date
    valid_end: date

    def __str__(self) -> str:
        return (f"train[{self.train_start}~{self.train_end}] "
                f"valid[{self.valid_start}~{self.valid_end}]")


@dataclass
class ParamScore:
    params: dict[str, Any]
    oos_scores: list[float] = field(default_factory=list)
    is_scores: list[float] = field(default_factory=list)
    detail: list[dict] = field(default_factory=list)

    @property
    def mean_oos(self) -> float:
        return float(np.mean(self.oos_scores)) if self.oos_scores else float("-inf")

    @property
    def std_oos(self) -> float:
        return float(np.std(self.oos_scores)) if len(self.oos_scores) > 1 else 0.0

    @property
    def worst_oos(self) -> float:
        return float(min(self.oos_scores)) if self.oos_scores else float("-inf")

    @property
    def overfit_gap(self) -> float:
        """训练窗与验证窗的表现差。差距越大越可疑。"""
        if not self.is_scores or not self.oos_scores:
            return 0.0
        return float(np.mean(self.is_scores)) - self.mean_oos

    def robust_score(self, *, stability_penalty: float = 0.5,
                     worst_weight: float = 0.3) -> float:
        """稳健得分：均值 − 波动惩罚 + 最差情形加权（关心尾部而不只是平均）。"""
        return (self.mean_oos
                - stability_penalty * self.std_oos
                + worst_weight * self.worst_oos)


@dataclass
class OptimizeResult:
    best: ParamScore | None = None
    all_scores: list[ParamScore] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    baseline: ParamScore | None = None
    accepted: bool = False
    reason: str = ""

    def report(self) -> str:
        lines = ["=" * 60,
                 f"Walk-forward 寻优  窗口={len(self.windows)}  参数组={len(self.all_scores)}",
                 "=" * 60]
        if self.baseline:
            lines.append(f"  基线: OOS={self.baseline.mean_oos:.4f} "
                         f"稳健={self.baseline.robust_score():.4f}")
        for ps in sorted(self.all_scores, key=lambda p: -p.robust_score())[:5]:
            lines.append(
                f"  {ps.params}  OOS={ps.mean_oos:+.4f}±{ps.std_oos:.4f} "
                f"最差={ps.worst_oos:+.4f} 过拟合差={ps.overfit_gap:+.4f} "
                f"稳健={ps.robust_score():+.4f}")
        lines.append(f"  结论: {'采纳' if self.accepted else '不采纳'} —— {self.reason}")
        return "\n".join(lines)


class WalkForwardOptimizer:
    """滚动窗口参数寻优。

    Parameters
    ----------
    evaluate : Callable[[dict, date, date], float]
        回调：给定参数与区间，返回该区间的绩效标量（越大越好，建议用夏普）。
        实际使用时通常包一层 BacktestEngine。
    """

    def __init__(self, evaluate: Callable[[dict, date, date], float], settings=None,
                 *, train_days: int | None = None, valid_days: int | None = None,
                 step_days: int | None = None, min_windows: int | None = None,
                 stability_penalty: float | None = None, min_improve: float | None = None,
                 max_overfit_gap: float | None = None):
        cfg = settings.section("evolution") if settings is not None else {}

        def pick(arg, key, default):
            """显式传参 > 配置 > 硬编码默认。

            顺序反过来（配置盖掉传参）是个隐蔽的坑：调用方明明写了 ``step_days=30``
            却被 yaml 里的 60 悄悄覆盖，测试与线上行为不一致。
            """
            return arg if arg is not None else cfg.get(key, default)

        self.evaluate = evaluate
        self.train_days = int(pick(train_days, "train_days", 180))
        self.valid_days = int(pick(valid_days, "valid_days", 60))
        self.step_days = int(pick(step_days, "step_days", 60))
        self.min_windows = int(pick(min_windows, "min_windows", 3))
        self.stability_penalty = float(pick(stability_penalty, "stability_penalty", 0.5))
        # 需超基线 min_improve 才换参
        self.min_improve = float(pick(min_improve, "min_improve", 0.10))
        self.max_overfit_gap = float(pick(max_overfit_gap, "max_overfit_gap", 1.0))
        self.max_param_step = float(cfg.get("max_param_step", 0.30))

    # ------------------------------------------------------------- 窗口切分
    def make_windows(self, start: date, end: date) -> list[Window]:
        out: list[Window] = []
        cur = start
        while True:
            tr_end = cur + timedelta(days=self.train_days)
            va_end = tr_end + timedelta(days=self.valid_days)
            if va_end > end:
                break
            out.append(Window(cur, tr_end, tr_end + timedelta(days=1), va_end))
            cur = cur + timedelta(days=self.step_days)
        return out

    @staticmethod
    def grid(space: dict[str, Sequence[Any]]) -> list[dict[str, Any]]:
        keys = list(space)
        return [dict(zip(keys, vals)) for vals in itertools.product(*(space[k] for k in keys))]

    # ---------------------------------------------------------------- 主流程
    def run(self, space: dict[str, Sequence[Any]], start: date, end: date,
            *, baseline: dict[str, Any] | None = None) -> OptimizeResult:
        windows = self.make_windows(start, end)
        res = OptimizeResult(windows=windows)
        if len(windows) < self.min_windows:
            res.reason = (f"可用窗口 {len(windows)} < 最少 {self.min_windows}，"
                          f"样本不足以判断稳健性")
            return res

        combos = self.grid(space)
        logger.info("Walk-forward 开始：%d 个参数组 × %d 个窗口", len(combos), len(windows))

        for params in combos:
            ps = ParamScore(params=params)
            for w in windows:
                try:
                    is_ = float(self.evaluate(params, w.train_start, w.train_end))
                    oos = float(self.evaluate(params, w.valid_start, w.valid_end))
                except Exception as exc:                # 单窗失败不毁全局
                    logger.warning("评估失败 %s @%s: %s", params, w, exc)
                    continue
                if not (np.isfinite(is_) and np.isfinite(oos)):
                    continue
                ps.is_scores.append(is_)
                ps.oos_scores.append(oos)
                ps.detail.append({"window": str(w), "is": is_, "oos": oos})
            if ps.oos_scores:
                res.all_scores.append(ps)

        if not res.all_scores:
            res.reason = "所有参数组评估均失败"
            return res

        if baseline is not None:
            res.baseline = next(
                (p for p in res.all_scores if p.params == baseline), None)

        ranked = sorted(res.all_scores,
                        key=lambda p: -p.robust_score(stability_penalty=self.stability_penalty))
        res.best = ranked[0]
        res.accepted, res.reason = self._decide(res)
        logger.info("Walk-forward 完成：%s", res.reason)
        return res

    # ---------------------------------------------------------------- 决策
    def _decide(self, res: OptimizeResult) -> tuple[bool, str]:
        best = res.best
        assert best is not None
        if best.overfit_gap > self.max_overfit_gap:
            return False, (f"最优参数过拟合嫌疑大（训练-验证差 {best.overfit_gap:+.3f} "
                           f"> {self.max_overfit_gap}）")
        if best.worst_oos < 0 and best.mean_oos < abs(best.worst_oos):
            return False, f"存在明显亏损窗口（最差 OOS={best.worst_oos:+.3f}），不稳健"
        if res.baseline is not None:
            b = res.baseline.robust_score(stability_penalty=self.stability_penalty)
            n = best.robust_score(stability_penalty=self.stability_penalty)
            need = abs(b) * self.min_improve
            if n - b < need:
                return False, (f"相对基线提升 {n - b:+.4f} 未达门槛 {need:.4f}，"
                               f"维持原参数（避免无谓换参）")
            return True, f"稳健得分 {b:+.4f} → {n:+.4f}，采纳新参数"
        return True, f"无基线，采纳稳健得分最高的参数（{best.robust_score():+.4f}）"

    # ------------------------------------------------------------ 变更限幅
    def clamp(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        """按配置的 ``max_param_step`` 限幅（实例版，实际调参走这个）。"""
        return self.clamp_change(old, new, max_step=self.max_param_step)

    @staticmethod
    def clamp_change(old: dict[str, Any], new: dict[str, Any],
                     max_step: float = 0.3) -> dict[str, Any]:
        """限制单次参数变更幅度，避免被一次拟合结果带跑偏。"""
        out = dict(old)
        for k, nv in new.items():
            ov = old.get(k)
            if isinstance(ov, (int, float)) and isinstance(nv, (int, float)) and ov:
                lo, hi = ov * (1 - max_step), ov * (1 + max_step)
                clamped = max(min(float(nv), max(lo, hi)), min(lo, hi))
                out[k] = type(ov)(round(clamped)) if isinstance(ov, int) else clamped
            else:
                out[k] = nv
        return out
