"""L2-a 选股漏斗：硬过滤 → 打分排序 → 候选池（设计 6.3）。"""

from .pipeline import CandidateSet, SelectionPipeline
from .ranker import RankResult, Ranker
from .screener import FunnelStage, ScreenResult, Screener

__all__ = [
    "Screener",
    "ScreenResult",
    "FunnelStage",
    "Ranker",
    "RankResult",
    "SelectionPipeline",
    "CandidateSet",
]
