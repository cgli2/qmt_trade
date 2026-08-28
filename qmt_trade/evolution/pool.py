"""策略池与资金调权（设计 L5 收尾）。

单一策略必然有失效期。这里维护一个**策略池**，按各策略近期的风险调整后表现
动态分配资金权重，并管理策略的生命周期。

与"简单地把钱给近期收益最高的策略"相比，这里刻意加了四道约束，
因为纯追逐近期收益是散户化的自杀式做法：

1. **半衰期加权**：近期样本权重高，但老样本不清零，避免被一周行情带跑；
2. **新策略先影子**：``SHADOW`` 状态只记账不给钱，跑满 ``promote_min_obs``
   且表现为正才转 ``ACTIVE``——杜绝"回测一好立刻上实盘"；
3. **权重限幅**：单次调权变动不超过 ``max_step``，且单策略权重有上限，
   避免资金在策略间来回甩；
4. **隔离与退休**：回撤或夏普跌破阈值先进 ``QUARANTINE``（不给钱但继续跑），
   连续 ``retire_after`` 次仍不合格才 ``RETIRED``——给策略留出均值回复的机会。

权重之和恒等于 1（全部策略都不合格时退化为等权现金策略 ``__cash__``）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Literal

import numpy as np

logger = logging.getLogger(__name__)

Status = Literal["SHADOW", "ACTIVE", "QUARANTINE", "RETIRED"]

#: 全部策略都不合格时的兜底"策略"：持币。它永远存在、永远不被淘汰。
CASH = "__cash__"


@dataclass
class StrategyRecord:
    """池中的一个策略。``returns`` 是按时间升序的周期收益率（小数）。"""

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    status: Status = "SHADOW"
    weight: float = 0.0
    returns: list[float] = field(default_factory=list)
    dates: list[date] = field(default_factory=list)
    #: 连续不合格次数，达到 retire_after 则退休
    strikes: int = 0
    note: str = ""

    @property
    def n(self) -> int:
        return len(self.returns)

    def add(self, r: float, when: date | None = None) -> None:
        self.returns.append(float(r))
        self.dates.append(when)

    def tail(self, k: int) -> list[float]:
        return self.returns[-k:] if k > 0 else list(self.returns)


@dataclass
class PoolMetrics:
    name: str
    n: int = 0
    mean: float = 0.0          # 半衰期加权平均收益
    vol: float = 0.0
    sharpe: float = 0.0        # 年化（按 periods_per_year 折算）
    max_dd: float = 0.0        # 窗口内历史最大回撤，正数=回撤幅度
    cur_dd: float = 0.0        # **当前**距峰值的回撤（恢复判定用）
    recent_sharpe: float = 0.0  # 近 recover_window 期的夏普（恢复判定用）
    hit: float = 0.0           # 胜率
    score: float = 0.0         # 综合得分（调权依据）

    def render(self) -> str:
        return (f"{self.name:<16} n={self.n:<4} sharpe={self.sharpe:+.2f} "
                f"dd={self.max_dd:.2%}(now {self.cur_dd:.2%}) "
                f"hit={self.hit:.0%} score={self.score:+.3f}")


@dataclass
class PoolDecision:
    name: str
    old_status: Status
    new_status: Status
    old_weight: float
    new_weight: float
    reason: str

    @property
    def changed(self) -> bool:
        return (self.old_status != self.new_status
                or abs(self.old_weight - self.new_weight) > 1e-9)

    def render(self) -> str:
        arrow = (f"{self.old_status}→{self.new_status}"
                 if self.old_status != self.new_status else self.new_status)
        return (f"{self.name:<16} [{arrow}] "
                f"w {self.old_weight:.1%}→{self.new_weight:.1%}  {self.reason}")


@dataclass
class RebalanceResult:
    asof: date
    weights: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, PoolMetrics] = field(default_factory=dict)
    decisions: list[PoolDecision] = field(default_factory=list)

    def report(self) -> str:
        lines = ["=" * 62, f"策略池调权 asof={self.asof}", "=" * 62]
        for m in sorted(self.metrics.values(), key=lambda x: -x.score):
            lines.append("  " + m.render())
        lines.append("-" * 62)
        for d in self.decisions:
            if d.changed:
                lines.append("  " + d.render())
        alloc = ", ".join(f"{k}={v:.0%}" for k, v in
                          sorted(self.weights.items(), key=lambda kv: -kv[1]) if v > 0)
        lines.append(f"  最终配置: {alloc or '全部持币'}")
        return "\n".join(lines)


class StrategyPool:
    """策略池。只做**权重分配与状态流转**，不碰下单——权重交给上层按比例切分资金。"""

    def __init__(self, settings=None, *, periods_per_year: int = 244):
        cfg = settings.section("evolution") if settings is not None else {}
        pool = cfg.get("pool", {}) if isinstance(cfg.get("pool"), dict) else {}
        self.periods_per_year = int(pool.get("periods_per_year", periods_per_year))
        self.window = int(pool.get("window", 120))            # 评估窗口（周期数）
        self.halflife = float(pool.get("halflife", 40))       # 半衰期加权
        self.min_obs = int(pool.get("min_obs", 20))           # 参与调权的最少样本
        self.promote_min_obs = int(pool.get("promote_min_obs", 40))  # 影子转正门槛
        self.max_weight = float(pool.get("max_weight", 0.5))  # 单策略权重上限
        self.min_weight = float(pool.get("min_weight", 0.05)) # 低于此值直接清零，避免碎权重
        self.max_step = float(pool.get("max_step", 0.20))     # 单次调权变动上限
        self.dd_penalty = float(pool.get("dd_penalty", 2.0))
        self.quarantine_dd = float(pool.get("quarantine_dd", 0.15))
        self.quarantine_sharpe = float(pool.get("quarantine_sharpe", -0.5))
        self.recover_sharpe = float(pool.get("recover_sharpe", 0.3))
        self.recover_window = int(pool.get("recover_window", 40))
        self.retire_after = int(pool.get("retire_after", 3))
        self.strategies: dict[str, StrategyRecord] = {}
        self._ensure_cash()
        # 种子权重（2026-08-16）：人工认可的独立策略（如回测三窗口验证的 trend_buy）
        # 以给定权重直接入池 ACTIVE，跳过纯影子期 —— 等价"纸面试用转正"的人工背书；
        # 之后由池子的 rebalance 正常接管（隔离/退休机制照常生效）。
        seed = pool.get("seed_weights") or {}
        if isinstance(seed, dict):
            for name, w in seed.items():
                try:
                    w = float(w)
                except (TypeError, ValueError):
                    continue
                if w > 0:
                    rec = self.register(name, status="ACTIVE")
                    rec.weight = min(w, self.max_weight)
                    rec.note = "种子权重（人工背书，等待 rebalance 接管）"

    # ---------------------------------------------------------------- 注册
    def _ensure_cash(self) -> None:
        if CASH not in self.strategies:
            self.strategies[CASH] = StrategyRecord(
                name=CASH, status="ACTIVE", weight=1.0, note="兜底持币")

    def register(self, name: str, *, params: dict | None = None,
                 status: Status = "SHADOW") -> StrategyRecord:
        if name == CASH:
            raise ValueError(f"{CASH} 为保留名")
        rec = self.strategies.get(name)
        if rec is None:
            rec = StrategyRecord(name=name, params=params or {}, status=status)
            self.strategies[name] = rec
            logger.info("策略入池 %s status=%s", name, status)
        return rec

    def record(self, name: str, ret: float, when: date | None = None) -> None:
        """记录一个周期的收益率。影子策略同样记录（这正是影子的意义）。"""
        rec = self.strategies.get(name) or self.register(name)
        rec.add(ret, when)

    def record_batch(self, name: str, rets: Iterable[float],
                     dates: Iterable[date] | None = None) -> None:
        ds = list(dates) if dates is not None else []
        for i, r in enumerate(rets):
            self.record(name, r, ds[i] if i < len(ds) else None)

    def retire(self, name: str, reason: str = "人工下线") -> None:
        rec = self.strategies.get(name)
        if rec and name != CASH:
            rec.status, rec.weight, rec.note = "RETIRED", 0.0, reason

    # ---------------------------------------------------------------- 评估
    def _decay(self, n: int) -> np.ndarray:
        if n <= 0:
            return np.zeros(0)
        if self.halflife <= 0:
            return np.ones(n)
        # 最新样本权重 1，往前每过一个半衰期权重减半
        age = np.arange(n - 1, -1, -1, dtype=float)
        return np.power(0.5, age / self.halflife)

    @staticmethod
    def _drawdowns(rets: list[float]) -> tuple[float, float]:
        """返回 (窗口内最大回撤, 当前回撤)，均为正数幅度。

        两个都要：**最大回撤**决定"曾经出过多大事"（触发隔离），
        **当前回撤**决定"现在是否已经走出来"（解除隔离）。
        只看最大回撤会让隔离永不解除——一次崩盘在窗口里就是永久污点。
        """
        if not rets:
            return 0.0, 0.0
        eq = np.cumprod(1.0 + np.asarray(rets, dtype=float))
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / np.where(peak <= 0, 1.0, peak)
        return float(np.max(dd)), float(dd[-1])

    #: 零波动时夏普数学上是 ±∞，这里截断成一个大而有限的值。
    #: 绝不能返回 0——那等于把"稳定赚钱"和"完全不赚钱"判成一回事，
    #: 会让持续盈利的策略永远解除不了隔离（这是实盘里真实踩过的坑）。
    SHARPE_CAP = 10.0

    def _ann(self, mu: float, sd: float) -> float:
        """年化夏普，带零波动兜底。"""
        if sd > 1e-12:
            return mu / sd * math.sqrt(self.periods_per_year)
        if abs(mu) <= 1e-12:
            return 0.0
        return math.copysign(self.SHARPE_CAP, mu)

    def _sharpe(self, rets: list[float]) -> float:
        if len(rets) < 2:
            return 0.0
        arr = np.asarray(rets, dtype=float)
        return self._ann(float(arr.mean()), float(arr.std()))

    def metrics(self, name: str) -> PoolMetrics:
        rec = self.strategies[name]
        rets = rec.tail(self.window)
        m = PoolMetrics(name=name, n=len(rets))
        if name == CASH:
            m.score = 0.0                      # 持币基准分恒为 0
            return m
        if not rets:
            m.score = float("-inf")
            return m
        arr = np.asarray(rets, dtype=float)
        w = self._decay(len(arr))
        w = w / w.sum()
        m.mean = float(np.dot(w, arr))
        var = float(np.dot(w, (arr - m.mean) ** 2))
        m.vol = math.sqrt(max(var, 0.0))
        m.sharpe = self._ann(m.mean, m.vol)
        m.max_dd, m.cur_dd = self._drawdowns(rets)
        m.recent_sharpe = self._sharpe(rec.tail(self.recover_window))
        m.hit = float(np.mean(arr > 0))
        # 样本不足时把得分往 0 收缩（贝叶斯味的收缩，防小样本吹牛）
        shrink = min(1.0, len(arr) / max(self.min_obs, 1))
        m.score = (m.sharpe - self.dd_penalty * m.max_dd) * shrink
        return m

    # ------------------------------------------------------------ 状态流转
    def _transition(self, rec: StrategyRecord, m: PoolMetrics) -> tuple[Status, str]:
        st = rec.status
        if st == "RETIRED":
            return st, "已退休"
        if st == "SHADOW":
            if m.n < self.promote_min_obs:
                return st, f"影子观察中 {m.n}/{self.promote_min_obs}"
            if m.score > 0 and m.max_dd <= self.quarantine_dd:
                rec.strikes = 0
                return "ACTIVE", f"影子期达标（score={m.score:+.3f}），转实盘"
            return st, f"影子期未达标（score={m.score:+.3f}），继续观察"

        bad = (m.max_dd > self.quarantine_dd) or (m.sharpe < self.quarantine_sharpe)
        if st == "ACTIVE":
            if m.n >= self.min_obs and bad:
                rec.strikes += 1
                return "QUARANTINE", (
                    f"触发隔离（dd={m.max_dd:.1%} sharpe={m.sharpe:+.2f}），"
                    f"strike {rec.strikes}/{self.retire_after}")
            rec.strikes = 0
            return st, "正常"

        # QUARANTINE：恢复判定看「当前回撤是否收敛 + 近期夏普是否转正」，
        # 而不是看窗口内的历史最大回撤（那是已经发生过的、不可改变的事）。
        if m.cur_dd <= self.quarantine_dd and m.recent_sharpe >= self.recover_sharpe:
            rec.strikes = 0
            return "ACTIVE", (f"恢复达标（当前回撤 {m.cur_dd:.1%}，"
                              f"近期 sharpe={m.recent_sharpe:+.2f}），解除隔离")
        rec.strikes += 1
        if rec.strikes >= self.retire_after:
            return "RETIRED", f"连续 {rec.strikes} 次不合格，退休"
        return st, f"仍在隔离 strike {rec.strikes}/{self.retire_after}"

    # ---------------------------------------------------------------- 调权
    def rebalance(self, asof: date) -> RebalanceResult:
        res = RebalanceResult(asof=asof)
        self._ensure_cash()
        old_status = {n: r.status for n, r in self.strategies.items()}
        old_weight = {n: r.weight for n, r in self.strategies.items()}

        # 1) 评估 + 状态流转
        reasons: dict[str, str] = {}
        for name, rec in self.strategies.items():
            m = self.metrics(name)
            res.metrics[name] = m
            new_status, why = self._transition(rec, m)
            rec.status, reasons[name] = new_status, why

        # 2) 只有 ACTIVE 且样本充足的策略参与分钱
        eligible = {n: res.metrics[n].score for n, r in self.strategies.items()
                    if n != CASH and r.status == "ACTIVE"
                    and res.metrics[n].n >= self.min_obs
                    and res.metrics[n].score > 0}

        raw = self._allocate(eligible)

        # 3) 变更限幅：加仓渐进（单次不超过 max_step），**减仓不限幅**。
        #    风控动作必须是即时的：一个策略被隔离/退休却还要三轮才把钱撤干净，
        #    等于在明知它坏掉之后继续送钱。上坡慢、下坡快，是刻意的不对称。
        target: dict[str, float] = {}
        for name, rec in self.strategies.items():
            if name == CASH:
                continue
            prev, want = old_weight.get(name, 0.0), raw.get(name, 0.0)
            if rec.status != "ACTIVE" or want <= 0.0:
                target[name] = 0.0                 # 出局即刻断粮
                continue
            step = max(-self.max_step, min(self.max_step, want - prev))
            v = max(0.0, min(self.max_weight, prev + step))
            target[name] = 0.0 if v < self.min_weight else v

        # 4) 归一化：超配则等比缩放；欠配的剩余全部给现金。
        #    先对策略权重定点到 6 位小数，**再**用 1 - Σ 反推现金，
        #    否则各自 round 之后总和会差出 1e-6 量级（钱对不上账是硬伤）。
        total = sum(target.values())
        if total > 1.0:
            target = {k: v / total for k, v in target.items()}
        target = {k: round(v, 6) for k, v in target.items()}
        excess = sum(target.values()) - 1.0
        if excess > 0 and target:                 # 舍入导致超配，从最大的那个扣回来
            top = max(target, key=lambda k: target[k])
            target[top] = max(0.0, round(target[top] - excess, 6))
        target[CASH] = max(0.0, round(1.0 - sum(target.values()), 6))

        for name, rec in self.strategies.items():
            rec.weight = target.get(name, 0.0)
            res.decisions.append(PoolDecision(
                name=name, old_status=old_status[name], new_status=rec.status,
                old_weight=old_weight.get(name, 0.0), new_weight=rec.weight,
                reason=reasons.get(name, "")))
        res.weights = {n: r.weight for n, r in self.strategies.items()}

        logger.info("策略池调权 asof=%s 活跃=%d 现金权重=%.1f%%", asof,
                    sum(1 for r in self.strategies.values() if r.status == "ACTIVE"),
                    res.weights.get(CASH, 0.0) * 100)
        return res

    def _allocate(self, scores: dict[str, float]) -> dict[str, float]:
        """得分 → 权重。用得分正比分配 + 单策略上限，再把溢出部分二次分配。

        不用 softmax：softmax 对得分尺度极其敏感，夏普 1.0 与 1.2 能分出 2 倍差距，
        对噪声太脆弱。正比分配更钝、更稳。
        """
        if not scores:
            return {}
        total = sum(scores.values())
        if total <= 0:
            return {}
        w = {k: v / total for k, v in scores.items()}
        # 迭代削峰：超上限的截断，多出来的按剩余得分再分配
        for _ in range(10):
            over = {k: v for k, v in w.items() if v > self.max_weight}
            if not over:
                break
            spill = sum(v - self.max_weight for v in over.values())
            for k in over:
                w[k] = self.max_weight
            rest = {k: scores[k] for k in w if k not in over}
            rest_total = sum(rest.values())
            if rest_total <= 0:
                break
            for k, s in rest.items():
                w[k] = min(self.max_weight, w[k] + spill * s / rest_total)
        return w

    # ---------------------------------------------------------------- 导出
    def snapshot(self) -> dict[str, Any]:
        """可直接落库/落盘的状态快照（P6 可复现）。

        收益历史只保留窗口内的部分 —— 这是评估唯一会用到的数据，
        多存无益还会把 system_state 撑大。
        """
        return {
            "strategies": {
                n: {"status": r.status, "weight": r.weight, "n": r.n,
                    "strikes": r.strikes, "params": r.params, "note": r.note,
                    "returns": r.returns[-self.window:],
                    "dates": [d.isoformat() if d else None
                              for d in r.dates[-self.window:]]}
                for n, r in self.strategies.items()
            }
        }

    def load(self, snap: dict[str, Any]) -> None:
        for n, d in (snap.get("strategies") or {}).items():
            rec = self.strategies.get(n) or StrategyRecord(name=n)
            rec.status = d.get("status", rec.status)
            rec.weight = float(d.get("weight", rec.weight))
            rec.strikes = int(d.get("strikes", rec.strikes))
            rec.params = d.get("params", rec.params)
            rec.note = d.get("note", rec.note)
            rets = d.get("returns")
            if rets:                                     # 收益历史断档 → 调权空转，必须恢复
                rec.returns = [float(x) for x in rets]
                ds = d.get("dates") or []
                rec.dates = [date.fromisoformat(x) if x else None for x in ds]
                rec.dates += [None] * max(0, len(rec.returns) - len(rec.dates))
            self.strategies[n] = rec
        self._ensure_cash()
