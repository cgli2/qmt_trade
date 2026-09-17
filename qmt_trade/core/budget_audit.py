"""仓位 / 额度参数的自洽性审计（2026-09-17「规则统一」）。

背景：项目里散落着一堆各自"看起来合法"的仓位参数——持仓数上限、单票权重上限、
目标持仓数、单笔委托上限、现金缓冲、Regime 总仓位上限、策略自己的底仓占比……
它们**单个都合规，组合起来却互相打架**，例如：

    max_positions=8 × max_single_weight=0.12 = 96%
    而 regime.max_position 最大只有 TREND_UP=80%
    → 任何市况下都不可能建满 8 只，8 这个数字是死的

    equal_weight=true + target_positions=8 → 每只目标 12.5%
    却被 portfolio.max_weight_pct=0.12 截到 12%
    → 叫"等权"，实际从来没等权过

这类矛盾过去只能靠盘中拒单反推（"总仓位已达 Regime 上限"拒了 1259 笔才被发现）。
本模块在**配置加载时**就把它们算出来并告警。

设计纪律：
- **只读**：绝不修改任何配置值，绝不改变交易行为，纯告警。
- **给方向**：每条告警都带上"当前值 → 建议值"，但不替用户拍板——
  仓位规模是策略意图，只有人能决定。
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def audit_position_budget(settings) -> list[str]:
    """检查仓位 / 额度参数之间是否自洽，返回人类可读的告警列表（可为空）。"""
    warns: list[str] = []

    g1 = settings.section("risk.gate1") or {}
    pf = settings.section("portfolio") or {}
    rg = settings.section("regime") or {}

    # ---- Regime 总仓位上限：外层天花板 ----
    caps_raw = rg.get("max_position")
    caps = {k: _f(v) for k, v in caps_raw.items()} if isinstance(caps_raw, dict) else {}
    cap_vals = [v for v in caps.values() if v > 0]
    cap_max = max(cap_vals) if cap_vals else 0.0
    cap_min = min(cap_vals) if cap_vals else 0.0

    max_positions = _i(g1.get("max_positions"), 8) or 8
    single_w = _f(g1.get("max_single_weight"), 0.15)
    pf_weight = _f(pf.get("max_weight_pct"), 0.15)
    target_pos = _i(pf.get("target_positions"), 8) or 8
    equal_weight = bool(pf.get("equal_weight", False))
    order_ratio = _f(g1.get("max_order_value_ratio"), 0.10)
    ind_w = _f(g1.get("max_industry_weight"), 0.30)

    # ---- 1) 持仓数 × 单票上限 vs Regime 天花板 ----
    if cap_max > 0 and max_positions * single_w > cap_max * 1.001:
        feasible = int(cap_max // single_w) if single_w > 0 else 0
        warns.append(
            f"[仓位不可达] max_positions({max_positions}) × max_single_weight"
            f"({single_w:.0%}) = {max_positions * single_w:.0%}，超过最宽松的 Regime "
            f"上限 {cap_max:.0%} —— 任何市况下都建不满 {max_positions} 只。"
            f"建议：max_positions 降到 {feasible}，或 max_single_weight 降到 "
            f"{cap_max / max_positions:.1%}（二选一，别同时改）")

    # ---- 2) 等权目标被单票上限截断（"等权"名不副实）----
    if equal_weight and target_pos > 0:
        ideal = 1.0 / target_pos
        if pf_weight > 0 and ideal > pf_weight * 1.001:
            warns.append(
                f"[等权失真] equal_weight=true 且 target_positions={target_pos} → 每只理想 "
                f"{ideal:.1%}，但 portfolio.max_weight_pct={pf_weight:.0%} 把它压到 "
                f"{pf_weight:.0%}，实际从未等权。建议：max_weight_pct 提到 ≥{ideal:.1%}，"
                f"或 target_positions 提到 ≥{math.ceil(1.0 / pf_weight) if pf_weight else 0}"
                f"（让 1/target_positions ≤ max_weight_pct）")

    # ---- 3) 单笔委托上限 < 单票上限（单票建满要分多笔）----
    if 0 < order_ratio < single_w:
        warns.append(
            f"[单笔<单票] max_order_value_ratio={order_ratio:.0%} 小于 "
            f"max_single_weight={single_w:.0%} —— 想把一只票建到上限至少要 "
            f"{math.ceil(single_w / order_ratio)} 次委托，而 OrderGuard 限频/冷却会把它"
            f"拖成跨分钟分批。建议：两者取齐（都设 {max(order_ratio, single_w):.0%}），"
            f"或明确接受分批并调大 OrderGuard 限额")

    # ---- 4) 两处"单票上限"是否一致（Gate-1 与 sizer 各读各的）----
    if abs(single_w - pf_weight) > 1e-6:
        warns.append(
            f"[单票上限不一致] risk.gate1.max_single_weight={single_w:.0%} 与 "
            f"portfolio.max_weight_pct={pf_weight:.0%} 不一致 —— Gate-1 按前者拦、"
            f"PositionSizer 按后者算股数，会出现「sizer 算出来、Gate-1 却拒掉」。"
            f"建议统一为同一个值")

    # ---- 5) 现金缓冲两处同名不同值 ----
    cash_usage = _f(pf.get("cash_usage_ratio"), 0.95)
    sizer_buf = 1.0 - cash_usage          # PositionSizer 实际用的是 1 - cash_usage_ratio
    risk_buf = _f(g1.get("cash_buffer"), 0.005)
    if abs(sizer_buf - risk_buf) > 1e-6:
        warns.append(
            f"[现金缓冲不一致] PositionSizer 用 1-cash_usage_ratio={sizer_buf:.1%}，"
            f"risk.gate1.cash_buffer={risk_buf:.1%} —— 同名概念两个数值。"
            f"建议统一（一般留 {max(sizer_buf, risk_buf):.1%} 即可）")

    # ---- 6) 行业上限 vs 单票上限（每行业能放几只）----
    if ind_w > 0 and single_w > 0 and ind_w < single_w:
        warns.append(
            f"[行业<单票] max_industry_weight={ind_w:.0%} 小于 max_single_weight="
            f"{single_w:.0%} —— 单只建满就必然突破行业上限，第一只就会被拒。"
            f"建议：行业上限 ≥ 单票上限（如 {max(ind_w, single_w * 2):.0%}）")
    elif ind_w > 0 and single_w > 0:
        per_industry = int(ind_w // single_w)
        if per_industry < 2:
            warns.append(
                f"[行业容量不足] max_industry_weight={ind_w:.0%} 只放得下 {per_industry} 只"
                f"单票({single_w:.0%}) —— 同一行业买第 2 只时就会被 Gate-1 拒掉"
                f"（{single_w:.0%}×2 > {ind_w:.0%}）。若你本来就不想同行业持仓，可忽略；"
                f"否则把行业上限提到 ≥{single_w * 2:.0%}")

    # ---- 7) ETF T+0 底仓意图 vs 单票上限 / Regime 天花板 ----
    etf = settings.section("strategies.etf_t0") or {}
    if etf.get("enabled"):
        base_frac = _f(etf.get("base_fraction"), 0.0)
        ov = etf.get("base_fraction_override") or {}
        syms = etf.get("symbols") or []
        fracs = {s: _f(ov.get(s), base_frac) for s in syms} if isinstance(ov, dict) else {}
        over = {s: v for s, v in fracs.items() if v > single_w + 1e-9}
        if over:
            detail = "、".join(f"{s}={v:.0%}" for s, v in over.items())
            warns.append(
                f"[底仓意图超单票上限] {detail} 超过 max_single_weight={single_w:.0%}，"
                f"实际会被压到 {single_w:.0%}（策略里还会再被 Regime 可用额度收敛一次）。"
                f"若确实要重仓，请先放宽 max_single_weight；否则把配置改成实际想要的值，"
                f"避免「配了 80% 却只建 12%」的错觉")
        total_intent = sum(fracs.values())
        if cap_min > 0 and total_intent > cap_max * 1.001:
            warns.append(
                f"[底仓意图超 Regime 天花板] ETF T+0 底仓意图合计 {total_intent:.0%}，"
                f"超过最宽松的 Regime 上限 {cap_max:.0%}；在 TREND_DOWN({cap_min:.0%}) 下"
                f"实际只能建到 {cap_min:.0%} 减去已有仓位。策略已会自动收敛，但日志会持续告警")

    return warns


_AUDITED = False


def audit_once(settings) -> list[str]:
    """进程内只审计一次并打日志（配置是单例，重复告警没有意义）。"""
    global _AUDITED
    if _AUDITED:
        return []
    _AUDITED = True
    try:
        warns = audit_position_budget(settings)
    except Exception as exc:                            # noqa: BLE001
        logger.warning("仓位参数自洽性审计失败: %s", exc)
        return []
    if warns:
        logger.warning("仓位/额度参数自洽性审计：发现 %d 处冲突（只告警，不自动修改）",
                       len(warns))
        for i, w in enumerate(warns, 1):
            logger.warning("  %d. %s", i, w)
    else:
        logger.info("仓位/额度参数自洽性审计：未发现冲突")
    return warns
