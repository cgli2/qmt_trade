"""持久化层：SQLite 连接封装 + 各实体仓储。

只暴露 ``Database`` 与 ``Repos``，业务层不直接写 SQL。
"""

from .db import Database
from .models import (
    ExperienceRepo,
    IntentRepo,
    LLMCallRepo,
    OrderRepo,
    PlanRepo,
    PositionRepo,
    Repos,
    RiskEventRepo,
    SnapshotRepo,
    SystemStateRepo,
    TradeRepo,
    init_db,
    new_id,
)

__all__ = [
    "Database", "Repos", "init_db", "new_id",
    "OrderRepo", "TradeRepo", "PositionRepo", "RiskEventRepo", "LLMCallRepo",
    "SystemStateRepo", "IntentRepo", "PlanRepo", "SnapshotRepo", "ExperienceRepo",
]
