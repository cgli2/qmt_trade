"""特征引擎：编排取数 → 算因子 → 截面标准化 → 加权合成。

对应设计 6.2.1 的落地。三个关键设计决策：

1. **打分用截面分位而非原始值**。不同因子量纲差 10 个数量级（成交额 vs 换手率），
   直接加权毫无意义。分位排名天然可比且抗厚尾。
2. **权重随 Regime 变化**。趋势市里动量因子有效、震荡市里反转和质量因子有效，
   固定权重等于在一半的时间里用错模型。
3. **缺失值不补 0 而是补中位分位（0.5）**。补 0 等于给缺数据的票判死刑，
   补均值又会让它虚高，0.5（中性）是唯一不引入偏差的选择。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from ..core.logging import get_logger
from ..datahub.pit import as_of_pre_open
from ..datahub.types import Adjust, Freq
from . import factors  # noqa: F401  导入即注册
from .base import CATEGORIES, FactorContext, neutralize, registry
from .regime import Regime, RegimeSnapshot

if TYPE_CHECKING:  # pragma: no cover
    from ..core.config import Settings
    from ..datahub.manager import DataHub

logger = get_logger("features.engine")


#: 各 Regime 下的因子类别权重（设计 6.2.1"权重按 Regime 动态调整"）
DEFAULT_CATEGORY_WEIGHTS: dict[Regime, dict[str, float]] = {
    Regime.TREND_UP: {
        "momentum": 0.40, "moneyflow": 0.25, "sentiment": 0.15,
        "fundamental": 0.12, "quality": 0.08,
    },
    Regime.RANGE: {
        "momentum": 0.22, "moneyflow": 0.18, "sentiment": 0.10,
        "fundamental": 0.28, "quality": 0.22,
    },
    Regime.TREND_DOWN: {
        "momentum": 0.10, "moneyflow": 0.15, "sentiment": 0.05,
        "fundamental": 0.35, "quality": 0.35,
    },
    Regime.RISK_OFF: {
        "momentum": 0.05, "moneyflow": 0.10, "sentiment": 0.05,
        "fundamental": 0.40, "quality": 0.40,
    },
}


@dataclass
class FeatureResult:
    """某一时点的全市场特征结果。"""

    asof: date
    #: 长表：date/symbol/行情列 + 各因子原始值 + 各因子分位 + 分类分 + score
    frame: pd.DataFrame
    factor_names: list[str]
    category_weights: dict[str, float]
    regime: Regime | None = None
    stats: dict = field(default_factory=dict)

    def top(self, n: int = 100) -> pd.DataFrame:
        return self.frame.nlargest(n, "score")

    def scores(self) -> pd.Series:
        return self.frame.set_index("symbol")["score"]


class FeatureEngine:
    def __init__(self, settings: "Settings", hub: "DataHub"):
        self.settings = settings
        self.hub = hub
        cfg = settings.section("features") or {}
        self.history_days = int(cfg.get("history_days", 300))
        self.enabled: list[str] | None = cfg.get("enabled_factors") or None
        self.neutralize_industry = bool(cfg.get("neutralize_industry", True))
        self.min_valid_ratio = float(cfg.get("min_valid_ratio", 0.30))
        self.category_weights = self._load_weights(cfg.get("category_weights") or {})
        self.factor_weights: dict[str, float] = dict(cfg.get("factor_weights") or {})

    def _load_weights(self, override: dict) -> dict[Regime, dict[str, float]]:
        out: dict[Regime, dict[str, float]] = {}
        for r in Regime:
            base = dict(DEFAULT_CATEGORY_WEIGHTS[r])
            base.update(override.get(r.value, {}) or {})
            total = sum(base.values()) or 1.0
            out[r] = {k: v / total for k, v in base.items()}
        return out

    # ------------------------------------------------------------ 取数
    def build_panel(
        self,
        symbols: Sequence[str],
        asof: date | datetime,
        *,
        history_days: int | None = None,
        start: date | None = None,
    ) -> pd.DataFrame:
        """构造因子计算用的长表 panel。

        ``asof`` 用**盘前时点**（09:00），保证当日日线不可见 —— 选股是在开盘前做的，
        不能用当天的收盘价。这是最容易犯的穿越错误。

        ``start`` 显式指定历史起点。回测里应当固定起点滚动 asof，否则每天的
        rolling 窗口起点都在变，pandas 增量累加会带来 1e-16 级的浮点抖动，
        虽然不影响收益，但会让"同一天的因子值"无法复现，破坏 P6 可复现性。
        """
        day = asof.date() if isinstance(asof, datetime) else asof
        cut = asof if isinstance(asof, datetime) else as_of_pre_open(day)
        days = history_days or self.history_days
        if start is None:
            start = day - pd.Timedelta(days=int(days * 1.6))
        panel = self.hub.get_bars(
            list(symbols),
            Freq.D1,
            start.date() if hasattr(start, "date") else start,
            day,
            Adjust.HFQ,
            asof=cut,
        )
        if panel.empty:
            return panel
        panel = panel.sort_values(["symbol", "date"]).reset_index(drop=True)
        # 附加行业信息，供中性化与行业动量因子使用
        try:
            infos = self.hub.get_instruments(list(panel["symbol"].unique()))
            ind = {i.symbol: (i.industry or "未知") for i in infos}
            panel["industry"] = panel["symbol"].map(ind).fillna("未知")
        except Exception as exc:  # noqa: BLE001
            logger.warning("行业信息缺失，跳过行业中性化: %s", exc)
            panel["industry"] = "未知"
        return panel

    # ------------------------------------------------------------ 计算
    def compute(
        self,
        symbols: Sequence[str],
        asof: date | datetime,
        *,
        regime: Regime | RegimeSnapshot | None = None,
        panel: pd.DataFrame | None = None,
        factor_names: Iterable[str] | None = None,
        ic_overrides: Mapping[str, float] | None = None,
        cat_weights_override: Mapping[str, float] | Mapping[str, Mapping[str, float]] | None = None,
    ) -> FeatureResult:
        day = asof.date() if isinstance(asof, datetime) else asof
        cut = asof if isinstance(asof, datetime) else as_of_pre_open(day)
        if panel is None:
            panel = self.build_panel(symbols, asof)
        if panel is None or panel.empty:
            logger.warning("panel 为空，返回空特征结果")
            return FeatureResult(day, pd.DataFrame(), [], {}, None)

        names = list(factor_names) if factor_names else (self.enabled or registry.names())
        # Phase 2（2026-08-12）：跳过 factor_weights 显式置 0 的因子 —— 正式禁用的
        # 死因子**不参与计算**。此前 moneyflow 5 个因子权重全 0，但 compute_all 仍会
        # 逐个调用 → 每天对全市场 4500+ 只触发 akshare 资金流逐票联网（12s/票超时
        # ≈ 15 小时卡顿），浪费且无意义。保留 hard_negative_flag：它是独立的一票
        # 否决风控（_rank_and_score 中不参与打分但可直接清零 score），必须照常计算。
        if not factor_names and self.factor_weights:
            zeroed = {n for n, w in self.factor_weights.items()
                      if float(w) == 0.0 and n != "hard_negative_flag"}
            if zeroed:
                names = [n for n in names if n not in zeroed]
        ctx = FactorContext(asof=cut, hub=self.hub, settings=self.settings)
        raw = registry.compute_all(panel, ctx, names)

        # 只保留每票最后一行（截面），因子已含历史信息
        merged = pd.concat([panel.reset_index(drop=True), raw.reset_index(drop=True)], axis=1)
        latest = (
            merged.sort_values(["symbol", "date"])
            .groupby("symbol", sort=False)
            .tail(1)
            .reset_index(drop=True)
        )

        used = self._drop_dead_factors(latest, list(raw.columns))
        rg = regime.regime if isinstance(regime, RegimeSnapshot) else regime
        # 策略预设覆盖：优先用 preset 提供的该 Regime 权重，否则回落默认。
        # 兼容两种形态：嵌套 {regime.value: {cat: w}} 与扁平 {cat: w}
        # （strategies.resolve_weights 的输出就是扁平形态，此前只认嵌套导致预设从未生效）。
        rgv = (rg or Regime.RANGE).value
        ov: dict[str, float] | None = None
        if cat_weights_override:
            inner = cat_weights_override.get(rgv)
            ov = (dict(inner) if isinstance(inner, Mapping)
                  else {str(c): float(v) for c, v in dict(cat_weights_override).items()})
        if ov is not None:
            base = self.category_weights[rg or Regime.RANGE]
            weights = {c: float(ov.get(c, base.get(c, 0.0))) for c in base}
            tot = sum(weights.values()) or 1.0
            weights = {c: v / tot for c, v in weights.items()}
        else:
            weights = self.category_weights[rg or Regime.RANGE]
        latest = self._rank_and_score(latest, used, regime, ic_overrides=ic_overrides,
                                      cat_weights_override=weights)
        stats = {
            "n_symbols": int(len(latest)),
            "n_factors": len(used),
            "dropped_factors": [c for c in raw.columns if c not in used],
            "score_mean": float(latest["score"].mean()) if len(latest) else 0.0,
            "score_std": float(latest["score"].std()) if len(latest) else 0.0,
        }
        logger.info(
            "特征计算完成 asof=%s 标的=%d 因子=%d(丢弃%d) regime=%s",
            day, stats["n_symbols"], stats["n_factors"],
            len(stats["dropped_factors"]), (rg or Regime.RANGE).value,
        )
        return FeatureResult(day, latest, used, weights, rg, stats)

    def _drop_dead_factors(self, df: pd.DataFrame, cols: list[str]) -> list[str]:
        """丢弃有效率过低的因子。

        一个因子如果 80% 的票都是 NaN（比如资金流数据源挂了），
        参与打分只会引入噪声：有数据的少数票会被这个维度主导。
        """
        keep = []
        for c in cols:
            valid = df[c].notna().mean() if len(df) else 0.0
            if valid >= self.min_valid_ratio:
                keep.append(c)
            else:
                logger.debug("因子 %s 有效率 %.1f%% 过低，本轮丢弃", c, valid * 100)
        return keep

    def _rank_and_score(
        self, df: pd.DataFrame, cols: list[str], regime: Regime | RegimeSnapshot | None,
        *, ic_overrides: Mapping[str, float] | None = None,
        cat_weights_override: Mapping[str, float] | None = None,
    ) -> pd.DataFrame:
        out = df.copy()
        if not cols:
            out["score"] = 0.5
            return out

        industry = out["industry"] if self.neutralize_industry and "industry" in out else None
        cat_of = {c: registry.meta(c).category for c in cols}

        # 1) 行业中性化 → 2) 截面分位
        for c in cols:
            v = out[c].astype(float)
            if industry is not None and cat_of[c] in ("momentum", "moneyflow", "sentiment"):
                # 估值/质量类不做行业中性：跨行业的 PE 差异本身就是有效信息
                v = neutralize(v, industry)
            out[f"{c}_q"] = v.rank(pct=True, na_option="keep")

        # 3) 类别内等权（或用配置的因子权重）→ 类别分
        #    ic_overrides：L5 复盘算出的因子 IC 回灌——持续负 IC 的因子在此被降权
        for cat in CATEGORIES:
            members = [c for c in cols if cat_of[c] == cat]
            if not members:
                continue
            raw = np.array([(ic_overrides.get(c) if ic_overrides and c in ic_overrides
                             else self.factor_weights.get(c, 1.0)) for c in members],
                           dtype=float)
            # 符号分离：负 IC 因子应反向使用（低原始值=优），权重取绝对值以保证归一化稳定。
            # 这正是"L5 复盘 IC 回灌"应有之义——IC 为负时不是降权而是把分位翻转为 (1-q)，
            # 使该因子变成反向（contra）信号；此前用正权重会把它"方向做反"，拖累选股。
            mag = np.abs(raw)
            sign = np.where(raw >= 0, 1.0, -1.0)
            w = mag / mag.sum() if mag.sum() else mag
            qs = out[[f"{c}_q" for c in members]].to_numpy(dtype=float)
            qs_signed = np.where(sign[None, :] > 0, qs, 1.0 - qs)
            # 逐票只用它自己非空的因子做加权，避免"缺数据=低分"的系统性偏差
            mask = ~np.isnan(qs_signed)
            wm = np.where(mask, w[None, :], 0.0)
            denom = wm.sum(axis=1)
            num = np.nansum(np.where(mask, qs_signed * w[None, :], np.nan), axis=1)
            out[f"cat_{cat}"] = np.where(denom > 0, num / np.maximum(denom, 1e-12), np.nan)

        # 4) 类别加权合成
        rg = regime.regime if isinstance(regime, RegimeSnapshot) else regime
        cw = dict(cat_weights_override) if cat_weights_override else self.category_weights[rg or Regime.RANGE]
        cat_cols = [f"cat_{c}" for c in CATEGORIES if f"cat_{c}" in out.columns]
        if not cat_cols:
            out["score"] = 0.5
            return out
        cw_arr = np.array([cw.get(c.replace("cat_", ""), 0.0) for c in cat_cols], dtype=float)
        vals = out[cat_cols].to_numpy(dtype=float)
        mask = ~np.isnan(vals)
        wm = np.where(mask, cw_arr[None, :], 0.0)
        denom = wm.sum(axis=1)
        num = np.nansum(np.where(mask, vals * cw_arr[None, :], 0.0), axis=1)
        # 全部类别都缺失 → 给中性分 0.5，不给 0（0 等于判死刑）
        out["score"] = np.where(denom > 0, num / np.maximum(denom, 1e-12), 0.5)

        # 5) 硬负面一票否决：无论其他因子多好，直接压到最低分
        if "hard_negative_flag" in out.columns:
            hit = out["hard_negative_flag"].fillna(0) < 0
            if hit.any():
                out.loc[hit, "score"] = 0.0
                logger.info("硬负面事件否决 %d 只标的", int(hit.sum()))
        return out.sort_values("score", ascending=False).reset_index(drop=True)
