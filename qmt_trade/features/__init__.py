"""L1 特征层：因子库、特征引擎、市场状态识别、因子有效性检验。"""

from .base import FactorContext, FactorMeta, registry
from .engine import FeatureEngine, FeatureResult
from .regime import Regime, RegimeDetector, RegimeSnapshot
from .validate import FactorReport, evaluate_all, evaluate_factor

__all__ = [
    "registry", "FactorContext", "FactorMeta",
    "FeatureEngine", "FeatureResult",
    "Regime", "RegimeDetector", "RegimeSnapshot",
    "FactorReport", "evaluate_factor", "evaluate_all",
]
