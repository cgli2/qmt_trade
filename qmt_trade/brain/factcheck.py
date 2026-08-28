"""事实校验（设计 6.4 / P3）。

LLM 的输出必须过这一关才能进入风控。任何必填缺失 / 数值越界 / 自相矛盾，
一律判无效 —— LLM **无权**绕过硬约束。配合「规则先行」：硬负面事件直接否决，
不等 LLM 慢慢想。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Sequence
from datetime import date

from .schemas import TradeIntent


@dataclass
class FactCheckResult:
    ok: bool
    issues: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> "FactCheckResult":
        self.ok = False
        self.issues.append(msg)
        return self


_ALLOWED_ACTIONS = {"BUY", "SELL", "HOLD", "REDUCE", "ADD"}


def check_intent(intent: TradeIntent, asof: date) -> FactCheckResult:
    r = FactCheckResult(ok=True)
    if intent.symbol == "" or intent.symbol is None:
        r.fail("symbol 缺失")
    if intent.action not in _ALLOWED_ACTIONS:
        r.fail(f"非法 action: {intent.action}")
    if not (0.0 <= intent.confidence <= 1.0):
        r.fail(f"confidence 越界: {intent.confidence}")
    if intent.stop_loss_value <= 0:
        r.fail("stop_loss_value 必须为正")
    if intent.stop_loss_type == "FIXED_PCT" and intent.stop_loss_value >= 0.5:
        r.fail(f"固定止损比例过大: {intent.stop_loss_value}")
    if not (0.0 <= intent.risk_budget_hint <= 1.5):
        r.fail(f"risk_budget_hint 越界: {intent.risk_budget_hint}")
    if not (0.0 <= intent.max_weight_hint <= 0.30):
        r.fail(f"max_weight_hint 越界: {intent.max_weight_hint}")
    if intent.max_holding_days <= 0:
        r.fail("max_holding_days 必须为正")
    if intent.valid_until < asof:
        r.fail(f"valid_until {intent.valid_until} 早于决策日 {asof}")
    # 卖出类动作必须有持仓逻辑（至少不能凭空买成卖无依据），这里只查字段完整性
    if intent.action in ("SELL", "REDUCE") and not intent.invalidation_checks and not intent.reasoning:
        r.fail("卖出动作缺少依据（invalidation/reasoning 至少一项）")
    return r


# ------------------------------------------------------------------ 幻觉检测
#: 常见的"非事实数字"白名单：百分号基数、常用窗口期、序号等，出现这些不算幻觉
_BENIGN = {0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 30.0, 50.0, 60.0, 100.0}


def _is_benign(x: float) -> bool:
    """年份 / 日期片段 / 常用序号不参与幻觉判定，避免"2026年中报"这类误报。"""
    if x in _BENIGN:
        return True
    if float(x).is_integer() and 1990 <= x <= 2100:   # 年份
        return True
    return False


def verify_numbers(cited: Sequence[float], factpack, *,
                   rel_tol: float = 0.05, abs_tol: float = 1e-6,
                   max_bad_ratio: float = 0.4) -> FactCheckResult:
    """把 LLM 输出里的数字与 FactPack 逐一比对（设计 6.4.2 的"事实校验器"）。

    这是自动交易里的必需安全阀：模型经常"顺手"编一个 PE 或涨幅出来。
    比对规则：每个被引用的数字，必须能在 FactPack 的数值集合里找到相对误差
    ≤ ``rel_tol`` 的匹配项；对不上的比例超过 ``max_bad_ratio`` 判定为幻觉。

    注意只做**告警级**判定（调用方据此打折信心），不直接否决——因为 LLM 也可能
    引用自己算出的合理衍生量（如 "止损 7% 对应 11.16 元"）。
    """
    r = FactCheckResult(ok=True)
    if not cited:
        return r
    pool = list(getattr(factpack, "numerics", {}).values())
    if not pool:
        return r
    # 衍生量：把事实数字的常见变换（百分比化、取绝对值）也纳入可匹配集合
    ext: list[float] = []
    for v in pool:
        ext.extend((v, abs(v), v * 100.0, v / 100.0))

    bad: list[float] = []
    for x in cited:
        if _is_benign(x):
            continue
        hit = any(abs(x - e) <= max(abs_tol, rel_tol * max(abs(e), 1e-9)) for e in ext)
        if not hit:
            bad.append(x)

    checked = max(1, len([x for x in cited if not _is_benign(x)]))
    ratio = len(bad) / checked
    if ratio > max_bad_ratio:
        r.fail(f"疑似幻觉：{len(bad)}/{checked} 个数字与事实卡片对不上 "
               f"（例如 {bad[:3]}）")
    return r
