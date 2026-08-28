"""因子基础设施：注册表、上下文、标准化工具。

设计约束（对应设计文档 6.2.1）：

1. **全部向量化**。因子在长表 panel（``date`` × ``symbol`` 两列 + 行情列）上一次算完，
   禁止 ``for symbol in symbols`` 逐票循环 —— 5000 只票的 Python 循环跑不动。
2. **严格 PIT**。因子只能用 ``panel`` 里已有的数据（panel 本身已被 DataHub 按 asof 切过），
   任何跨行"向后看"的操作（``shift(-1)``、``rolling(...).shift(-n)``）都是穿越。
   ``FactorRegistry.compute_all`` 会在每个因子算完后做一次自检。
3. **方向统一**。所有因子约定 **越大越好**。像 PE 这种越小越好的，在因子内部取负，
   不要留给下游打分器去记哪个正哪个负 —— 那是 bug 温床。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Callable, Iterable

import numpy as np
import pandas as pd

from ..core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from ..datahub.manager import DataHub

logger = get_logger("features.base")

#: 因子类别，用于分组加权与归因
CATEGORIES = ("momentum", "moneyflow", "fundamental", "sentiment", "quality")


@dataclass
class FactorContext:
    """因子计算上下文。

    ``panel`` 是主输入（长表）；需要额外数据（财务/资金流/新闻）的因子从 ``hub`` 取，
    但**必须**带上 ``asof``，否则会穿越。
    """

    asof: datetime | date
    hub: "DataHub | None" = None
    settings: object | None = None
    #: 额外数据缓存，避免同一批因子重复取数
    extras: dict[str, object] = field(default_factory=dict)

    def cached(self, key: str, loader: Callable[[], object]) -> object:
        if key not in self.extras:
            try:
                self.extras[key] = loader()
            except Exception as exc:  # noqa: BLE001 - 附加数据缺失不应阻断主链路
                logger.warning("因子附加数据 %s 获取失败，降级为空: %s", key, exc)
                self.extras[key] = None
        return self.extras[key]


@dataclass(frozen=True)
class FactorMeta:
    name: str
    category: str
    description: str
    #: 计算该因子所需的最小历史交易日数，不足则该票因子为 NaN
    min_periods: int = 20
    #: 是否需要额外数据源（缺失时可跳过而非报错）
    needs_extra: bool = False


class FactorRegistry:
    """因子注册表。用装饰器登记，引擎按名字取用。"""

    def __init__(self) -> None:
        self._factors: dict[str, tuple[FactorMeta, Callable]] = {}

    def register(
        self,
        name: str,
        category: str,
        description: str = "",
        *,
        min_periods: int = 20,
        needs_extra: bool = False,
    ):
        if category not in CATEGORIES:
            raise ValueError(f"未知因子类别 {category}，可选 {CATEGORIES}")

        def deco(fn: Callable[[pd.DataFrame, FactorContext], pd.Series]):
            if name in self._factors:
                raise ValueError(f"因子重名: {name}")
            meta = FactorMeta(name, category, description or fn.__doc__ or "", min_periods, needs_extra)
            self._factors[name] = (meta, fn)
            return fn

        return deco

    # ------------------------------------------------------------ 查询
    def __contains__(self, name: str) -> bool:
        return name in self._factors

    def names(self, category: str | None = None) -> list[str]:
        if category is None:
            return list(self._factors)
        return [n for n, (m, _) in self._factors.items() if m.category == category]

    def meta(self, name: str) -> FactorMeta:
        return self._factors[name][0]

    def all_meta(self) -> list[FactorMeta]:
        return [m for m, _ in self._factors.values()]

    # ------------------------------------------------------------ 计算
    def compute(self, name: str, panel: pd.DataFrame, ctx: FactorContext) -> pd.Series:
        meta, fn = self._factors[name]
        try:
            out = fn(panel, ctx)
        except Exception as exc:  # noqa: BLE001 - 单因子失败不拖垮整批
            logger.warning("因子 %s 计算失败，置 NaN: %s", name, exc)
            return pd.Series(np.nan, index=panel.index, name=name)
        if not isinstance(out, pd.Series):
            out = pd.Series(out, index=panel.index)
        out = out.reindex(panel.index)
        out.name = name
        return out.replace([np.inf, -np.inf], np.nan)

    def compute_all(
        self, panel: pd.DataFrame, ctx: FactorContext, names: Iterable[str] | None = None
    ) -> pd.DataFrame:
        cols = list(names) if names is not None else self.names()
        data = {n: self.compute(n, panel, ctx) for n in cols if n in self._factors}
        missing = [n for n in cols if n not in self._factors]
        if missing:
            logger.warning("以下因子未注册，已忽略: %s", missing)
        out = pd.DataFrame(data, index=panel.index)
        return out


#: 全局注册表
registry = FactorRegistry()


# ---------------------------------------------------------------- 截面工具
def cross_sectional_rank(
    df: pd.DataFrame, by: str = "date", *, pct: bool = True
) -> pd.DataFrame:
    """按日期做截面排名分位（0~1，越大越好）。

    用排名而非 z-score 是刻意选择：A 股因子分布普遍厚尾，z-score 会被极值主导，
    排名分位天然抗异常值，也让不同量纲的因子可直接加权求和。
    """
    return df.groupby(df[by] if by in df.columns else df.index.get_level_values(by)).rank(
        pct=pct, na_option="keep"
    )


def winsorize(s: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    """分位缩尾。用于 z-score 前置处理。"""
    if s.dropna().empty:
        return s
    lo, hi = s.quantile(lower), s.quantile(upper)
    return s.clip(lo, hi)


def zscore(s: pd.Series, *, winsor: bool = True) -> pd.Series:
    v = winsorize(s) if winsor else s
    std = v.std()
    if not np.isfinite(std) or std == 0:
        return pd.Series(0.0, index=s.index)
    return (v - v.mean()) / std


def neutralize(values: pd.Series, groups: pd.Series) -> pd.Series:
    """行业中性化：减去所属行业均值。

    不做中性化的话，因子打分会退化成"选出当下最强的那个行业"，
    组合集中度失控，这正是设计里要求行业分散约束的原因之一。
    """
    if groups is None or groups.isna().all():
        return values
    grp_mean = values.groupby(groups).transform("mean")
    return values - grp_mean.fillna(values.mean())


def safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    out = a / b.replace(0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan)


def pct_change_n(s: pd.Series, n: int) -> pd.Series:
    prev = s.shift(n)
    return safe_div(s - prev, prev.abs())
