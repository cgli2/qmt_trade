"""交易成本模型（设计 6.8.4）。回测与实盘共用同一套计算，杜绝失真（P7）。

佣金/印花税/过户费是精确规则；滑点用分档模型（委托量占近 20 日均量比 +
波动率 + 方向）。M1 阶段滑点函数偏保守，M3 用真实成交标定。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..core.trading import Side


class SlippageModel(str, Enum):
    FIXED = "FIXED"          # 固定滑点（M1 保守起步）
    VOLUME_RATIO = "VOLUME_RATIO"  # 按委托量占均量比分档


@dataclass
class CostModel:
    commission_rate: float = 0.00025     # 成交额比例
    commission_min: float = 5.0          # 单笔最低佣金
    stamp_tax_rate: float = 0.0005       # 印花税（仅卖出）
    transfer_rate: float = 0.00001       # 过户费（双边）
    slippage_model: SlippageModel = SlippageModel.FIXED
    fixed_slippage: float = 0.002        # 默认 0.2%（买高卖低）
    # 分档滑点参数：基准滑点 + 量比惩罚
    slip_base: float = 0.001
    slip_per_ratio: float = 0.01         # 量比每增加 1（即委托量=均量）滑点 +1%

    def commission(self, amount: float) -> float:
        return max(self.commission_min, amount * self.commission_rate)

    def stamp_tax(self, amount: float) -> float:
        # 卖出方向才收；调用方需按 side 决定
        return amount * self.stamp_tax_rate

    def transfer(self, amount: float) -> float:
        return amount * self.transfer_rate

    def slippage(self, side: Side, ref_price: float, *, volume_ratio: float = 0.0) -> float:
        """返回滑点金额（占 ref_price 的比例，0~+）。买入为正向偏移（更贵），卖出为负向。"""
        if self.slippage_model is SlippageModel.FIXED:
            frac = self.fixed_slippage
        else:
            frac = self.slip_base + self.slip_per_ratio * max(0.0, volume_ratio)
            frac = min(frac, 0.02)  # 滑点封顶 2%
        # 买入滑点使成交价更高（+），卖出使成交价更低（-）
        return frac if side is Side.BUY else -frac

    def fill_price(self, side: Side, ref_price: float, *, volume_ratio: float = 0.0) -> float:
        slip = self.slippage(side, ref_price, volume_ratio=volume_ratio)
        return round(ref_price * (1 + slip), 4)

    def total_fee(self, side: Side, amount: float) -> float:
        fee = self.commission(amount) + self.transfer(amount)
        if side is Side.SELL:
            fee += self.stamp_tax(amount)
        return fee

    @classmethod
    def from_settings(cls, settings) -> "CostModel":
        cfg = settings.section("execution.costs")
        return cls(
            commission_rate=float(cfg.get("commission_rate", 0.00025)),
            commission_min=float(cfg.get("commission_min", 5.0)),
            stamp_tax_rate=float(cfg.get("stamp_duty_rate", 0.0005)),
            transfer_rate=float(cfg.get("transfer_fee_rate", 0.00001)),
            fixed_slippage=float(cfg.get("base_slippage", 0.002)),
            slip_base=float(cfg.get("base_slippage", 0.001)),
        )
