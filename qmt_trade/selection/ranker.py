"""L1 因子打分排序 + 行业分散约束（设计 6.3 第二级漏斗）。

输入是 ``FeatureEngine`` 打完分的截面（含 ``score`` 与 ``industry``），
输出 Top N 候选池，交给 L2 的 LLM 深度研判。

为什么要行业分散约束：
    综合分是同一套因子算出来的，同一行业的票因子表现天然同涨同跌。
    不加约束时 Top100 经常被一两个当红行业占掉七八成 —— 表面是"选股"，
    实际是"押注单一行业"，一个政策利空就团灭。单行业 ≤ 15 是个软性的
    风险预算，把行业集中度的决定权从"因子巧合"手里拿回来。

被行业配额挤掉的高分票会记录在 ``crowded_out`` 里。这个信息很重要：
复盘时看到某只票涨得好却没进池，能立刻区分是"分不够"还是"行业满了"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from ..core.logging import get_logger

logger = get_logger("selection.ranker")

_UNKNOWN_INDUSTRY = "未知"


@dataclass
class RankResult:
    asof: date
    selected: list[str]
    #: 入选明细：symbol/score/industry/rank/raw_rank
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    #: 因行业配额被挤掉的票：[(symbol, industry, score, raw_rank)]
    crowded_out: list[tuple] = field(default_factory=list)
    #: 因低于最低分位门槛被剔除的数量（主门槛）
    below_percentile: int = 0
    #: 因低于绝对分底线被剔除的数量（附加门槛，默认不生效）
    below_score: int = 0
    industry_dist: dict[str, int] = field(default_factory=dict)
    relaxed: int = 0
    stats: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.selected)

    @property
    def below_threshold(self) -> int:
        """兼容旧字段名：低于任一门槛的总数。"""
        return self.below_percentile + self.below_score

    def report(self) -> str:
        top_ind = sorted(self.industry_dist.items(), key=lambda kv: -kv[1])[:6]
        lines = [
            f"L1 排序 asof={self.asof} 入选 {self.n} 只"
            f"（分位门槛剔除 {self.below_percentile}，绝对门槛剔除 {self.below_score}，"
            f"行业挤出 {len(self.crowded_out)}"
            + (f"，放宽补位 {self.relaxed}" if self.relaxed else "")
            + "）",
            "  行业分布: " + ", ".join(f"{k}×{v}" for k, v in top_ind) if top_ind else "  行业分布: -",
        ]
        if self.crowded_out:
            sample = ", ".join(
                f"{s}({ind} #{rk} {sc:.3f})" for s, ind, sc, rk in self.crowded_out[:5]
            )
            lines.append(f"  被行业配额挤出（前5）: {sample}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "asof": str(self.asof), "n": self.n, "selected": list(self.selected),
            "below_percentile": self.below_percentile, "below_score": self.below_score,
            "crowded_out": [list(x) for x in self.crowded_out],
            "industry_dist": dict(self.industry_dist), "relaxed": self.relaxed,
            "stats": dict(self.stats),
        }


class Ranker:
    def __init__(self, settings):
        cfg = settings.section("selection.ranker") or {}
        self.top_n = int(cfg.get("top_n", 100))
        self.max_per_industry = int(cfg.get("max_per_industry", 15))
        #: 行业配额导致取不满时，是否放宽约束补位。
        #: 默认开：宁可行业略集中，也不要候选池空到没法交易；但补位数量会记入 stats。
        self.relax_if_short = bool(cfg.get("relax_if_short", True))
        #: 补位时允许的行业上限倍数（1.5 → 15 只放宽到 22 只），不设为无穷是为了保底分散
        self.relax_factor = float(cfg.get("relax_factor", 1.5))

    def rank(
        self,
        scored: pd.DataFrame,
        *,
        asof: date,
        top_n: int | None = None,
        max_per_industry: int | None = None,
        min_score: float | None = None,
        min_percentile: float | None = None,
        universe: list[str] | None = None,
    ) -> RankResult:
        """对打分截面排序并施加行业分散约束。

        :param scored: 至少含 ``symbol``/``score`` 列的截面（``industry`` 可选）。
        :param min_percentile: **主门槛**——截面分位下限。只有综合分排在全市场
                              前 ``(1-min_percentile)`` 的票才保留。对分布形状不敏感，
                              是推荐的门槛表达方式（详见 regime.DEFAULT_MIN_PERCENTILE 注释）。
        :param min_score: **附加门槛**——绝对分底线（默认 0 = 不生效）。
                          用于"全市场都很烂时仍要一票否决"的场景。
        :param universe: 只在这个集合内排序（通常是 L0 硬过滤的输出）。
        """
        if scored is None or scored.empty or "score" not in scored.columns:
            logger.warning("排序输入为空或缺少 score 列 asof=%s", asof)
            return RankResult(asof=asof, selected=[])

        n_target = int(top_n if top_n is not None else self.top_n)
        cap = int(max_per_industry if max_per_industry is not None else self.max_per_industry)

        df = scored.copy()
        if universe is not None:
            allow = set(universe)
            df = df[df["symbol"].isin(allow)]
        n_universe = len(df)

        df = df.dropna(subset=["score"])

        below_pct, below_sc = 0, 0
        # ---- 主门槛：截面分位 ----
        if min_percentile is not None and 0 <= float(min_percentile) <= 1:
            df["_pct"] = df["score"].rank(pct=True)
            before = len(df)
            df = df[df["_pct"] >= float(min_percentile)]
            below_pct = before - len(df)
        # ---- 附加门槛：绝对分底线 ----
        if min_score is not None and float(min_score) > 0:
            before = len(df)
            df = df[df["score"] >= float(min_score)]
            below_sc = before - len(df)

        if df.empty:
            mp = f"{min_percentile:.2f}" if min_percentile is not None else "NA"
            ms = f"{min_score:.2f}" if min_score is not None else "NA"
            logger.info(
                "排序后候选为空 asof=%s（分位门槛 %s 剔除 %d，绝对门槛 %s 剔除 %d）",
                asof, mp, below_pct, ms, below_sc,
            )
            return RankResult(asof=asof, selected=[], below_percentile=below_pct,
                              below_score=below_sc, stats={"n_universe": n_universe})

        if "industry" not in df.columns:
            df["industry"] = _UNKNOWN_INDUSTRY
        df["industry"] = df["industry"].fillna("").replace("", _UNKNOWN_INDUSTRY)

        # symbol 作为二级排序键，保证同分时结果可复现（P6）
        df = df.sort_values(["score", "symbol"], ascending=[False, True]).reset_index(drop=True)
        df["raw_rank"] = df.index + 1

        selected, crowded, counts = self._greedy_pick(df, n_target, cap)

        relaxed = 0
        if len(selected) < n_target and self.relax_if_short and crowded:
            hard_cap = max(cap + 1, int(cap * self.relax_factor))
            extra, crowded = self._fill_short(df, selected, counts, n_target, hard_cap, crowded)
            relaxed = len(extra)
            selected.extend(extra)
            if relaxed:
                logger.info(
                    "行业约束导致取不满（%d/%d），放宽单行业上限 %d→%d 补位 %d 只",
                    len(selected) - relaxed, n_target, cap, hard_cap, relaxed,
                )

        sel_set = set(selected)
        out = df[df["symbol"].isin(sel_set)].copy()
        # 保持贪心选择的顺序（即按分数），再赋最终名次
        out["_ord"] = out["symbol"].map({s: i for i, s in enumerate(selected)})
        out = out.sort_values("_ord").drop(columns="_ord").reset_index(drop=True)
        out["rank"] = out.index + 1

        dist = out["industry"].value_counts().to_dict()
        logger.info(
            "L1 排序 asof=%s %d → %d（分位门槛剔除 %d，绝对门槛剔除 %d，行业挤出 %d，补位 %d）",
            asof, n_universe, len(selected), below_pct, below_sc, len(crowded), relaxed,
        )
        return RankResult(
            asof=asof, selected=selected, frame=out, crowded_out=crowded,
            below_percentile=below_pct, below_score=below_sc, industry_dist=dist,
            relaxed=relaxed,
            stats={
                "n_universe": n_universe, "n_scored": len(df), "target": n_target,
                "cap": cap, "score_min": float(out["score"].min()) if len(out) else None,
                "score_max": float(out["score"].max()) if len(out) else None,
            },
        )

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _greedy_pick(df: pd.DataFrame, n_target: int, cap: int):
        """按分数降序贪心取票，行业满配额则跳过。"""
        selected: list[str] = []
        crowded: list[tuple] = []
        counts: dict[str, int] = {}
        syms = df["symbol"].tolist()
        inds = df["industry"].tolist()
        scores = df["score"].tolist()
        raw_ranks = df["raw_rank"].tolist()

        for sym, ind, sc, rk in zip(syms, inds, scores, raw_ranks):
            if len(selected) >= n_target:
                break
            if counts.get(ind, 0) >= cap:
                crowded.append((sym, ind, float(sc), int(rk)))
                continue
            selected.append(sym)
            counts[ind] = counts.get(ind, 0) + 1
        return selected, crowded, counts

    @staticmethod
    def _fill_short(df, selected, counts, n_target, hard_cap, crowded):
        """放宽行业上限，从被挤出的票里按分数补位。"""
        chosen = set(selected)
        extra: list[str] = []
        still_out: list[tuple] = []
        for sym, ind, sc, rk in crowded:
            if len(selected) + len(extra) >= n_target:
                still_out.append((sym, ind, sc, rk))
                continue
            if sym in chosen or counts.get(ind, 0) >= hard_cap:
                still_out.append((sym, ind, sc, rk))
                continue
            extra.append(sym)
            chosen.add(sym)
            counts[ind] = counts.get(ind, 0) + 1
        return extra, still_out
