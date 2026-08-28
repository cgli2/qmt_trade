"""因子有效性检验：IC / IR / 分层回测。

设计 6.2.1 要求："新因子必须通过检验才能进入打分"。这是防止因子库无限膨胀、
最终变成一堆噪声加权的唯一手段。

指标含义：
- **IC**（Information Coefficient）：因子值与未来收益的截面相关系数。
  用 Spearman（秩相关）而非 Pearson，理由同打分用分位：抗厚尾。
- **IR**（Information Ratio）：IC 均值 / IC 标准差。衡量因子稳定性。
  IC 均值 0.05 但忽正忽负的因子，不如 IC 均值 0.03 但一直为正的。
- **分层回测**：按因子值分 N 组，看各组未来收益是否单调。
  单调性比 IC 更能说明问题 —— IC 高但不单调，往往是极值驱动的假象。

**这里计算未来收益是合法的**：这是离线因子研究，不是实盘决策。
但必须严格区分：``forward_return`` 只能出现在本模块，
任何在 ``features/factors/`` 里出现的 ``shift(-n)`` 都是 bug。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..core.logging import get_logger

logger = get_logger("features.validate")


@dataclass
class FactorReport:
    name: str
    ic_mean: float
    ic_std: float
    ir: float
    ic_positive_ratio: float
    n_periods: int
    layer_returns: list[float] = field(default_factory=list)
    monotonic: bool = False
    top_bottom_spread: float = 0.0
    coverage: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """准入标准。宁可严一点，因子少而精好过多而杂。"""
        return (
            abs(self.ic_mean) >= 0.02
            and abs(self.ir) >= 0.30
            and self.n_periods >= 20
            and self.coverage >= 0.50
            and self.top_bottom_spread > 0
        )

    @property
    def reject_reason(self) -> str:
        """未通过时给出**具体**原因，而不是让人对着一行数字猜。"""
        if self.passed:
            return ""
        rs = []
        if self.n_periods < 20:
            rs.append(f"有效截面仅{self.n_periods}期(<20)")
        if self.coverage < 0.50:
            rs.append(f"覆盖率{self.coverage:.0%}(<50%)")
        if abs(self.ic_mean) < 0.02:
            rs.append(f"IC过弱{self.ic_mean:+.4f}")
        if abs(self.ir) < 0.30:
            rs.append(f"IR过低{self.ir:+.2f}")
        if self.top_bottom_spread <= 0:
            rs.append(f"多空差非正{self.top_bottom_spread:+.2%}")
        return "; ".join(rs + self.notes)

    def summary(self) -> str:
        flag = "PASS" if self.passed else "FAIL"
        line = (
            f"[{flag}] {self.name:<22} IC={self.ic_mean:+.4f} IR={self.ir:+.2f} "
            f"正IC率={self.ic_positive_ratio:.0%} 多空差={self.top_bottom_spread:+.2%} "
            f"覆盖={self.coverage:.0%} N={self.n_periods}"
        )
        reason = self.reject_reason
        return f"{line}\n{'':>8}└─ {reason}" if reason else line

    def to_dict(self) -> dict:
        return {
            "name": self.name, "ic_mean": self.ic_mean, "ic_std": self.ic_std,
            "ir": self.ir, "ic_positive_ratio": self.ic_positive_ratio,
            "n_periods": self.n_periods, "layer_returns": self.layer_returns,
            "monotonic": self.monotonic, "top_bottom_spread": self.top_bottom_spread,
            "coverage": self.coverage, "passed": self.passed,
            "notes": self.notes, "reject_reason": self.reject_reason,
        }


def forward_return(panel: pd.DataFrame, periods: int = 5, price_col: str = "close") -> pd.Series:
    """未来 N 日收益。**仅供离线因子检验使用，禁止在因子计算中调用。**"""
    g = panel.groupby("symbol", sort=False)[price_col]
    fut = g.shift(-periods)
    cur = panel[price_col]
    return (fut / cur.replace(0, np.nan) - 1).replace([np.inf, -np.inf], np.nan)


def _pearson_np(x: np.ndarray, y: np.ndarray) -> float:
    """纯 numpy 的 Pearson 相关。

    刻意不走 ``Series.corr`` —— pandas 的 ``method="spearman"`` 底层依赖
    ``scipy.stats.spearmanr``，为了一个秩相关拖进 scipy 依赖不划算，而且
    scipy 每次调用的开销在"34 因子 × 400 个交易日"的量级下相当可观。
    秩相关 = 先 rank 再 Pearson，自己算既无依赖又快一个量级。
    """
    n = x.size
    if n < 2:
        return float("nan")
    xm = x - x.mean()
    ym = y - y.mean()
    denom = np.sqrt((xm * xm).sum() * (ym * ym).sum())
    if not np.isfinite(denom) or denom < 1e-12:
        return float("nan")
    return float((xm * ym).sum() / denom)


def compute_ic(
    panel: pd.DataFrame,
    factor_col: str,
    *,
    periods: int = 5,
    method: str = "spearman",
    min_samples: int = 10,
) -> pd.Series:
    """逐日截面 IC 序列。

    ``method="spearman"``（默认）先在每个截面内做 rank 再算相关，抗厚尾；
    ``method="pearson"`` 直接用原值。
    """
    df = panel[["date", "symbol", factor_col]].copy()
    df["_fwd"] = forward_return(panel, periods)
    df = df.dropna(subset=[factor_col, "_fwd"])
    if df.empty:
        return pd.Series(dtype=float, name=f"IC_{factor_col}")

    if method == "spearman":
        g = df.groupby("date", sort=False)
        df["_x"] = g[factor_col].rank()
        df["_y"] = g["_fwd"].rank()
    else:
        df["_x"] = df[factor_col].astype(float)
        df["_y"] = df["_fwd"].astype(float)

    ics: dict = {}
    for day, sub in df.groupby("date", sort=True):
        if len(sub) < min_samples:
            continue
        x = sub["_x"].to_numpy(dtype=float)
        y = sub["_y"].to_numpy(dtype=float)
        # 只有"常数列"才真的无法算相关。
        # 注意不要把门槛设成 nunique<3 —— 二值因子（涨停标记、硬负面 flag 之类）
        # 与连续收益的秩相关等价于 Mann-Whitney 检验，统计上完全有效，
        # 一刀切会让所有 flag 类因子的 IC 全部为空，看起来像"因子坏了"。
        if np.unique(x).size < 2 or np.unique(y).size < 2:
            continue
        v = _pearson_np(x, y)
        if np.isfinite(v):
            ics[day] = v
    return pd.Series(ics, name=f"IC_{factor_col}", dtype=float)


def layered_backtest(
    panel: pd.DataFrame, factor_col: str, *, periods: int = 5, n_layers: int = 5
) -> list[float]:
    """分层回测：按因子值分 N 组，返回各组平均未来收益（低分组 → 高分组）。"""
    df = panel[["date", "symbol", factor_col]].copy()
    df["_fwd"] = forward_return(panel, periods)
    df = df.dropna(subset=[factor_col, "_fwd"])
    if df.empty:
        return []
    out = np.zeros(n_layers)
    counts = np.zeros(n_layers)
    for _, g in df.groupby("date", sort=True):
        if len(g) < n_layers * 3:
            continue
        try:
            labels = pd.qcut(g[factor_col].rank(method="first"), n_layers, labels=False)
        except ValueError:
            continue
        for i in range(n_layers):
            sel = g.loc[labels == i, "_fwd"]
            if len(sel):
                out[i] += float(sel.mean())
                counts[i] += 1
    with np.errstate(invalid="ignore", divide="ignore"):
        res = np.where(counts > 0, out / np.maximum(counts, 1), np.nan)
    return [float(x) for x in res]


def evaluate_factor(
    panel: pd.DataFrame, factor_col: str, *, periods: int = 5, n_layers: int = 5
) -> FactorReport:
    ic = compute_ic(panel, factor_col, periods=periods)
    layers = layered_backtest(panel, factor_col, periods=periods, n_layers=n_layers)
    ic_mean = float(ic.mean()) if len(ic) else 0.0
    ic_std = float(ic.std()) if len(ic) > 1 else 0.0
    ir = ic_mean / ic_std if ic_std > 1e-12 else 0.0
    pos_ratio = float((ic > 0).mean()) if len(ic) else 0.0
    spread = (layers[-1] - layers[0]) if len(layers) >= 2 else 0.0
    mono = bool(len(layers) >= 3 and all(
        layers[i] <= layers[i + 1] for i in range(len(layers) - 1)
    ))
    col = panel[factor_col] if factor_col in panel else pd.Series(dtype=float)
    coverage = float(col.notna().mean()) if len(col) else 0.0

    # 诊断：区分"因子本身有问题"和"样本不够"。前者要改代码，后者只需拉长回看窗口。
    notes: list[str] = []
    nuniq = int(col.nunique(dropna=True))
    if nuniq <= 1:
        notes.append("因子在样本内是常数，无截面区分度")
    elif nuniq <= 2:
        notes.append(f"二值因子(取值{nuniq}种)，IC 解释力有限")
    if len(ic) == 0 and coverage > 0:
        notes.append("无任何可用截面：检查样本天数或单日样本量(<10只)")

    return FactorReport(
        name=factor_col, ic_mean=ic_mean, ic_std=ic_std, ir=ir,
        ic_positive_ratio=pos_ratio, n_periods=int(len(ic)),
        layer_returns=layers, monotonic=mono,
        top_bottom_spread=float(spread), coverage=coverage, notes=notes,
    )


def evaluate_all(
    panel: pd.DataFrame, factor_cols: list[str], *, periods: int = 5
) -> list[FactorReport]:
    reports = [evaluate_factor(panel, c, periods=periods) for c in factor_cols if c in panel]
    return sorted(reports, key=lambda r: abs(r.ir), reverse=True)


def correlation_matrix(panel: pd.DataFrame, factor_cols: list[str]) -> pd.DataFrame:
    """因子间相关性（秩相关）。相关性 > 0.8 的因子对说明信息高度重叠，应该删掉一个。

    同样用 rank + Pearson 规避 scipy 依赖。注意这里是**混合截面**的粗略估计，
    严格做法应逐日算完再平均；但用于"发现明显冗余"这个目的，粗略版足够。
    """
    cols = [c for c in factor_cols if c in panel]
    if not cols:
        return pd.DataFrame()
    ranked = panel[cols].apply(lambda s: s.rank())
    return ranked.corr(method="pearson")


def redundant_pairs(panel: pd.DataFrame, factor_cols: list[str], threshold: float = 0.8):
    corr = correlation_matrix(panel, factor_cols).abs()
    out = []
    cols = list(corr.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = corr.iloc[i, j]
            if np.isfinite(v) and v >= threshold:
                out.append((cols[i], cols[j], float(v)))
    return sorted(out, key=lambda x: -x[2])
