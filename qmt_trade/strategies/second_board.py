"""二板龙头战法（策略实验室·方向二，陈小群模式）。

核心逻辑：不追首板（胜率不足 30%）、不接三板以上（风险远大于收益），只做二板。
选股 = 昨日涨停 + **连板恰好 = 2** + 热门板块前 N + 开盘涨幅 2%~8%（T 日开盘买入）。
与方向一（打板）的差异仅在连板区间（=2 而非 ≥2）与**无**周末/节假日过滤。

离场与方向一同引擎（LimitUpBacktester）：
涨停延续持有，否则次日开盘卖出；硬止损 -7%；时间止损 5 日。

配置：config/settings.yaml::strategies.second_board
"""
from __future__ import annotations

from dataclasses import dataclass

from .limit_up import LimitUpBacktester, LimitUpConfig

__all__ = ["SecondBoardConfig", "SecondBoardBacktester"]


@dataclass
class SecondBoardConfig(LimitUpConfig):
    # 二板铁律：只做连板=2（首板不追、三板以上不接）
    min_boards: int = 2
    max_boards: int = 2
    # 方向二无周末/节前过滤（与方向一区分）
    exclude_weekend: bool = False
    exclude_holiday: bool = False
    hot_sector_top_n: int = 5
    # 2026-08-16 P2 迭代：灾难止损 12%（同打板）、弱市空仓 MA20 继承基类默认
    stop_pct: float = 0.12
    max_hold_days: int = 5
    require_non_oneword_prev: bool = True


class SecondBoardBacktester(LimitUpBacktester):
    """二板龙头 = 打板引擎 + 连板=2 限定。"""

    sid = "second_board"
    config_class = SecondBoardConfig
