#!/usr/bin/env python
"""Walk-forward 样本外定权 —— 替代"同窗口 IC 拟合"的过拟合做法（F5 修复）。

背景
----
``config/settings.yaml`` 的 ``factor_weights`` 此前用回测**同一段**窗口的 IC 拟合
（注释明示"来源：diag 区间 2026-06-01~08-10"，与测试区间相同），等于"用答案训练、
用同一份答案考试"（样本内过拟合）。本脚本把**定权窗口（train）与验证窗口（test）
严格分开**：只在 train 窗口算 IC/IR 推导权重，test 窗口仅用于验证权重是否在
样本外仍然成立。

规则（与 V7 同款，避免换一套规则引入新偏差）
---------------------------------------------
- 覆盖率 ≥ engine.min_valid_ratio（默认 0.30）且 N>0 的因子：
    权重 = |IR|，符号 = IC 符号（IC<0 → 负权重 → 引擎翻转分位作 contra 反向信号）
- 覆盖率不足 / N=0 / 超时未测的因子：权重 = 0（死亡因子，不占位）
- 输出可直接粘贴进 settings.yaml 的 ``factor_weights:`` 块，并写 reports/ 存档。

用法
----
    # 基本：train 定权 + test 验证（--mode paper 真实数据，需 akshare/tushare 联网）
    python scripts/walk_forward_ic.py \
        --train-start 2024-01-01 --train-end 2025-12-31 \
        --test-start 2026-01-01 --test-end 2026-08-10

    # 只定权不验证；或带上联网因子（基本面/资金流/新闻，单个卡住会超时跳过）
    python scripts/walk_forward_ic.py --train-start ... --train-end ... --no-validate
    python scripts/walk_forward_ic.py --train-start ... --train-end ... --include-extra

    # 加速抽样（800 只仍有代表性）
    python scripts/walk_forward_ic.py --train-start ... --train-end ... --max-symbols 800

注：本脚本只读、只算、只写 reports/ 下的权重存档，不修改 settings.yaml、不交易。
    修改 settings.yaml 由你审阅后手动执行。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# 让脚本在「python scripts/walk_forward_ic.py」直接运行时能 import 到 qmt_trade 与 diag_factor_ic
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

logger = logging.getLogger("walk_forward")


def derive_weights(ic_df: pd.DataFrame, engine: FeatureEngine) -> dict[str, float]:
    """按 V7 规则从 train 窗口 IC 表推导因子权重。

    ic_df 列：factor / category / IC / IR / 正IC率 / 多空差 / 覆盖率 / N
    """
    min_valid = engine.min_valid_ratio
    weights: dict[str, float] = {}
    for _, r in ic_df.iterrows():
        name, ic, ir, cov, n = r["factor"], float(r["IC"]), float(r["IR"]), float(r["覆盖率"]), int(r["N"])
        if n <= 0 or np.isnan(ic) or cov < min_valid:
            weights[name] = 0.0          # 死亡/未测/覆盖不足 → 不占位
        elif ic > 0:
            weights[name] = round(ir, 3)  # 正向：量级 |IR|，符号 +
        else:
            weights[name] = round(-abs(ir), 3)  # 负 IC → 反向（contra）信号
    return weights


def weights_yaml(weights: dict[str, float], *, train: str, test: str,
                 periods: int, extra_note: str = "") -> str:
    """生成可直接粘贴进 settings.yaml 的 factor_weights YAML 块。"""
    pos = {k: v for k, v in weights.items() if v > 0}
    neg = {k: v for k, v in weights.items() if v < 0}
    dead = {k: v for k, v in weights.items() if v == 0.0}
    lines = [
        "  # ─────────────────────────────────────────────────────────────────────",
        f"  # Walk-forward 权重（F5，样本外定权）：train {train} ~ {test}，前向 {periods} 日",
        "  # 规则：量级=|IR|、符号=IC 符号（负 IC→contra 反向）；覆盖率<min_valid_ratio/N=0 → 0",
        "  # ⚠ 此块由 scripts/walk_forward_ic.py 生成，供审阅后替换旧权重（旧权重为样本内拟合）",
        extra_note,
        "  factor_weights:",
    ]
    for k, v in sorted(pos.items(), key=lambda x: -x[1]):
        lines.append(f"    {k}: {v:.3f}")
    lines.append("    # —— 反向（IC<0 → contra）——")
    for k, v in sorted(neg.items(), key=lambda x: x[1]):
        lines.append(f"    {k}: {v:.3f}")
    lines.append("    # —— 死亡因子（覆盖率不足/N=0/超时，置 0）——")
    for k in sorted(dead):
        lines.append(f"    {k}: 0.0")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # diag 仅用于读取模块级常量（DEFAULT_EXTRA_TIMEOUT）。本函数体执行时才加载，
    # 不影响模块级纯函数 derive_weights/weights_yaml 在仅 numpy/pandas 的最小环境被测试。
    import diag_factor_ic as diag

    ap = argparse.ArgumentParser(description="Walk-forward 样本外因子定权（F5）")
    ap.add_argument("--train-start", required=True, help="定权窗口开始 YYYY-MM-DD")
    ap.add_argument("--train-end", required=True, help="定权窗口结束 YYYY-MM-DD")
    ap.add_argument("--test-start", default=None, help="验证窗口开始（默认不验证）")
    ap.add_argument("--test-end", default=None, help="验证窗口结束（默认今天）")
    ap.add_argument("--config", default=None, help="配置文件，默认 config/settings.yaml")
    ap.add_argument("--mode", default="paper", help="数据源模式 sim/paper/live，默认 paper")
    ap.add_argument("--warmup", type=int, default=250, help="因子预热天数")
    ap.add_argument("--periods", type=int, default=5, help="IC 前向收益天数")
    ap.add_argument("--max-symbols", type=int, default=None, help="限制标的数量加速")
    ap.add_argument("--include-extra", action="store_true",
                    help="也尝试联网因子（基本面/资金流/新闻）")
    ap.add_argument("--extra-timeout", type=int, default=diag.DEFAULT_EXTRA_TIMEOUT)
    ap.add_argument("--no-validate", action="store_true", help="跳过 test 窗口验证")
    args = ap.parse_args(argv)

    train_start = date.fromisoformat(args.train_start)
    train_end = date.fromisoformat(args.train_end)
    test_start = date.fromisoformat(args.test_start) if args.test_start else None
    test_end = date.fromisoformat(args.test_end) if args.test_end else date.today()
    if train_start >= train_end:
        raise SystemExit("--train-start 必须早于 --train-end")
    if test_start is not None and test_start <= train_end:
        raise SystemExit("验证窗口必须严格晚于定权窗口（walk-forward 纪律）")

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # 其余重依赖延迟导入：仅在真正构建上下文时才加载 qmt_trade 包（避免 import 即拉起整个包，
    # 也让 derive_weights / weights_yaml 等纯函数能在最小环境（仅 numpy/pandas）下被单元测试）
    from qmt_trade.app import build_context
    from qmt_trade.core.config import Settings
    from qmt_trade.features import factors  # 导入即注册所有因子
    from qmt_trade.features.base import registry
    from qmt_trade.features.engine import FeatureEngine

    settings = Settings.load(args.config) if args.config else None
    with build_context(args.mode, settings=settings) as ctx:
        hub = ctx.hub
        engine = FeatureEngine(ctx.settings, hub)

        infos = hub.get_instruments()
        universe = list(infos.keys()) if isinstance(infos, dict) else \
            [getattr(i, "symbol", str(i)) for i in (infos or [])]
        if args.max_symbols:
            universe = universe[: args.max_symbols]
        if not universe:
            raise SystemExit("标的和为空（数据源不可用？）")

        # ---- 1) train 窗口：算 IC/IR ----
        logger.info("=== 定权窗口 %s ~ %s（标的 %d）===", train_start, train_end, len(universe))
        panel = diag._load_panel(hub, universe, train_start, train_end, args.warmup)
        ctx_factor = diag.FactorContext(asof=train_end, hub=hub, settings=ctx.settings)
        extra_set = {n for n in registry.names() if registry.meta(n).needs_extra}
        price_names = [n for n in registry.names() if n not in extra_set]
        compute_names = list(price_names)
        if args.include_extra:
            compute_names += list(extra_set)

        raw: dict[str, pd.Series] = {}
        status: dict[str, str] = {}
        had_timeout = False
        for n in compute_names:
            is_extra = n in extra_set
            to = args.extra_timeout if is_extra else diag.PRICE_TIMEOUT
            res = diag._compute_factor_safe(n, panel, ctx_factor, to)
            if isinstance(res, pd.Series):
                raw[n] = res
                status[n] = "OK"
            else:
                status[n] = str(res)
                if "TIMEOUT" in str(res):
                    had_timeout = True
        if not raw:
            raise SystemExit("训练窗口一个因子都没算出来（检查数据源）")
        # 关键：把算好的因子列并回面板，否则 _factor_ic→evaluate_all 用 `if c in panel`
        # 过滤时会全部落空，返回空报告并在 sort_values("IR") 处 KeyError（与 diag_factor_ic 同口径）。
        computed = pd.DataFrame(raw, index=panel.index)
        panel = pd.concat([panel.reset_index(drop=True), computed.reset_index(drop=True)], axis=1)
        ic_df = diag._factor_ic(panel, list(computed.columns), args.periods)
        ic_df["状态"] = ic_df["factor"].map(status)

        weights = derive_weights(ic_df, engine)
        logger.info("有效因子 %d / 死亡置零 %d", sum(1 for v in weights.values() if v != 0.0),
                    sum(1 for v in weights.values() if v == 0.0))

        # ---- 2) 可选：test 窗口验证（样本外 IC 一致性）----
        test_ic: pd.DataFrame | None = None
        if test_start is not None and not args.no_validate:
            logger.info("=== 验证窗口 %s ~ %s（样本外）===", test_start, test_end)
            panel_t = diag._load_panel(hub, universe, test_start, test_end, args.warmup)
            ctx_t = diag.FactorContext(asof=test_end, hub=hub, settings=ctx.settings)
            raw_t: dict[str, pd.Series] = {}
            for n in list(computed.columns):
                res = diag._compute_factor_safe(n, panel_t, ctx_t, args.extra_timeout)
                if isinstance(res, pd.Series):
                    raw_t[n] = res
            if raw_t:
                computed_t = pd.DataFrame(raw_t, index=panel_t.index)
                panel_t = pd.concat([panel_t.reset_index(drop=True), computed_t.reset_index(drop=True)], axis=1)
                test_ic = diag._factor_ic(panel_t, list(computed_t.columns), args.periods)
                test_ic = test_ic.set_index("factor")[["IC", "IR", "覆盖率", "N"]].rename(
                    columns={"IC": "test_IC", "IR": "test_IR",
                             "覆盖率": "test_覆盖率", "N": "test_N"})

        # ---- 3) 汇总表 + 输出 ----
        out_df = ic_df[["factor", "category", "IC", "IR", "正IC率", "多空差", "覆盖率", "N", "状态"]].copy()
        out_df["权重"] = out_df["factor"].map(weights)
        if test_ic is not None:
            out_df = out_df.join(test_ic, on="factor")
        out_df = out_df.sort_values("权重", key=lambda s: s.abs(), ascending=False)

        yaml_block = weights_yaml(
            weights, train=f"{train_start}~{train_end}",
            test=f"{test_start}~{test_end}" if test_start else "（未验证）",
            periods=args.periods,
            extra_note=f"  # 生成：{date.today().isoformat()}，mode={args.mode}"
                       f"{', include-extra' if args.include_extra else ''}")

        report_path = _ROOT / "reports" / f"walk_forward_ic_{train_start}_{test_start or 'x'}_{test_end}.md"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"# Walk-forward 因子权重（F5 样本外定权）\n\n")
            f.write(f"- 定权窗口：{train_start} ~ {train_end}（前向 {args.periods} 日）\n")
            f.write(f"- 验证窗口：{test_start} ~ {test_end}\n" if test_start else "- 验证窗口：未验证\n")
            f.write(f"- 数据模式：{args.mode}\n\n## 因子 IC 与权重\n\n")
            f.write(out_df.to_markdown(index=False))
            f.write("\n\n## 权重 YAML（审阅后替换 settings.yaml 的 factor_weights）\n\n```yaml\n")
            f.write(yaml_block)
            f.write("\n```\n")
            if test_ic is not None:
                merged = out_df.dropna(subset=["IC", "test_IC"])
                if len(merged) > 2:
                    corr = merged["IC"].corr(merged["test_IC"])
                    f.write(f"\n## 样本外一致性\n\n- train IC 与 test IC 相关：{corr:.3f}"
                            f"（>0.3 视为方向稳定；<0 说明该窗口因子失效）\n")

        print("\n" + "=" * 60)
        print(out_df.to_string(index=False))
        print("\n" + "=" * 60)
        print("权重 YAML（复制到 config/settings.yaml 的 factor_weights: 下）：\n")
        print(yaml_block)
        print(f"\n已写入报告：{report_path}")

        if had_timeout:
            sys.stdout.flush()
            logger.warning("存在因子超时（后台线程未回收），强制退出进程避免挂起")
            os._exit(0)
        return 0


if __name__ == "__main__":
    sys.exit(main())
