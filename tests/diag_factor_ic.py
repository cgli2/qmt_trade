"""因子 IC / 覆盖率 / Regime 轨迹诊断脚本（Phase 0，已加固防卡死）。

用途
----
在你**真实的回测环境**跑一次，定位"策略为什么亏"：

1. 各因子的 IC / IR / 多空差 / 覆盖率（直接看哪些因子 IC 为负，应降权或删除）。
2. 每个因子类别（动量/资金流/情绪/基本面/质量）**实际有多少因子喂进了打分**——
   这是验证"非价格因子整路缺失、策略退化成纯高 Beta 动量"假设的关键。
3. 区间每天的 Regime 分类轨迹，看是否长期黏在 TREND_UP 而满仓穿越下跌。

为什么必须跑这个
----------------
V0→V4 一直在调止损/入场/预设，但 A/B 表显示（策略 -8% vs 基准 -2.94%、Sharpe≈-4.8、
V4 止损反而更差）问题在**信号本身（因子有没有正 IC）**而非参数。在拿到真实 IC 之前
调任何参数都是盲调。本脚本复用系统已有的 ``features/validate.py`` 与 ``DataHub``，
不引入新逻辑、不碰实盘。

防卡死设计（v2）
---------------
老版本一次 ``compute_all`` 把 34 个因子**同步全算**，其中基本面/资金流/新闻类因子
会走 tushare/akshare **联网取数**；一旦网络挂起（无 token / 无网 / 慢），整个进程
永久阻塞、连报告都出不来（这就是一跑就"卡死 Ctrl+C"的原因）。

v2 改法：
* **因子级隔离 + 超时**：每个因子在独立线程里算，超时（默认 90s）即跳过并标记，
  绝不拖垮整次运行。Windows 无 ``signal.alarm``，故用线程池 ``future.result(timeout)``。
* **默认只算价格类因子（不联网）**：动量/波动/技术共 16 个因子全部来自行情面板，
  不碰任何网络，因此默认运行**不可能卡死**，且已足够回答"价格因子有没有 IC"。
* **联网因子默认跳过**，加 ``--include-extra`` 才尝试；其中任一个挂起只会单独超时，
  其余照常出报告。
* 仅当确实发生了超时（有挂起线程未回收）时才在结尾 ``os._exit(0)`` 强制退出，
  避免进程卡在残留网络线程上；正常结束走常规退出，数据源能正常清理。

用法
----
    # 默认：只算价格因子，绝不会卡，约 1~2 分钟出 IC 报告
    python scripts/diag_factor_ic.py --start 2026-06-01 --end 2026-08-10

    # 连联网因子也测（会逐个联网；任一个卡住 90s 后自动跳过，不影响其余）
    python scripts/diag_factor_ic.py --start 2026-06-01 --end 2026-08-10 --include-extra

    # 加速：抽样 800 只仍有代表性；--extra-timeout 调大/调小联网容忍
    python scripts/diag_factor_ic.py --start 2026-06-01 --end 2026-08-10 --max-symbols 800
    python scripts/diag_factor_ic.py --start 2026-06-01 --end 2026-08-10 --include-extra --extra-timeout 120

注：本脚本只读、不写、不交易。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import logging
import os
import sys
from datetime import date, timedelta

# 让脚本在「python scripts/diag_factor_ic.py」直接运行时也能找到 qmt_trade 包
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from qmt_trade.app import build_context
from qmt_trade.core.config import Settings
from qmt_trade.features import factors  # 导入即注册所有因子
from qmt_trade.features.base import FactorContext, registry, CATEGORIES
from qmt_trade.features.engine import FeatureEngine
from qmt_trade.features.regime import Regime, RegimeDetector
from qmt_trade.features.validate import evaluate_all
from qmt_trade.datahub.types import Freq, Adjust

logger = logging.getLogger("diag")

#: 价格类因子（不需要联网）的超时上限；向量化计算通常秒级完成，给宽松值防极端慢。
PRICE_TIMEOUT = 300
#: 联网因子（needs_extra）的超时上限；卡住就跳过，不阻塞整体。
DEFAULT_EXTRA_TIMEOUT = 90


def _compute_factor_safe(name: str, panel: pd.DataFrame, ctx: FactorContext, timeout: int):
    """在独立线程里算单个因子，超时即返回状态字符串（不抛异常、不阻塞）。

    返回 ``pd.Series`` 表示成功；返回 ``"TIMEOUT"`` / ``"ERROR:<msg>"`` 表示失败。
    注意：超时后线程仍在后台跑，故调用方在结尾按需 ``os._exit`` 强制退出。
    """
    ex = cf.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(lambda: registry.compute(name, panel, ctx))
    try:
        res = fut.result(timeout=timeout)
        ex.shutdown(wait=False)
        return res
    except cf.TimeoutError:
        ex.shutdown(wait=False)  # 留后台线程；稍后 os._exit 收尾
        logger.warning("因子 %s 计算超时(%ss)，跳过（疑似数据源挂起）", name, timeout)
        return "TIMEOUT"
    except Exception as exc:  # noqa: BLE001
        ex.shutdown(wait=False)
        logger.warning("因子 %s 计算异常，跳过: %s", name, exc)
        return f"ERROR:{exc}"


def _load_panel(hub, universe, start: date, end: date, warmup: int) -> pd.DataFrame:
    """构造因子计算的原始长表（与回测 FeatureEngine.build_panel 同口径）。"""
    fixed_start = start - timedelta(days=warmup)
    logger.info("取数区间 %s ~ %s，标的 %d 只", fixed_start, end, len(universe))
    panel = hub.get_bars(
        list(universe), Freq.D1, fixed_start, end, Adjust.HFQ, asof=end,
    )
    if panel is None or panel.empty:
        raise SystemExit("行情面板为空，无法诊断（检查数据源）")
    # 附加行业信息（因子中性化需要）
    try:
        infos = hub.get_instruments(list(panel["symbol"].unique()))
        ind = {i.symbol: (i.industry or "未知") for i in infos}
        panel = panel.copy()
        panel["industry"] = panel["symbol"].map(ind).fillna("未知")
    except Exception as exc:  # noqa: BLE001
        logger.warning("行业信息缺失：%s", exc)
        panel["industry"] = "未知"
    return panel.sort_values(["symbol", "date"]).reset_index(drop=True)


def _factor_ic(panel: pd.DataFrame, computed_names: list[str], periods: int) -> pd.DataFrame:
    """逐因子 IC。直接用系统自带的 evaluate_all（离线 forward_return 合法）。"""
    logger.info("计算 %d 个因子的 IC（前向 %d 日收益）…", len(computed_names), periods)
    reports = evaluate_all(panel, computed_names, periods=periods)
    rows = []
    for r in reports:
        rows.append({
            "factor": r.name,
            "category": registry.meta(r.name).category if registry.meta(r.name) else "?",
            "IC": round(r.ic_mean, 4),
            "IR": round(r.ir, 3),
            "正IC率": round(r.ic_positive_ratio, 3),
            "多空差": round(r.top_bottom_spread, 4),
            "覆盖率": round(r.coverage, 3),
            "N": r.n_periods,
            "PASS": "Y" if r.passed else "-",
        })
    if not rows:
        # 调用方未把因子列并回 panel（如 walk_forward_ic 早期 bug）→ evaluate_all 全过滤掉。
        # 显式报错，避免 sort_values("IR") 抛晦涩的 KeyError: 'IR'。
        raise ValueError("因子 IC 报告为空：没有任何因子列进入评分（确认传入的 panel 已包含因子列）")
    return pd.DataFrame(rows).sort_values("IR", key=lambda s: s.abs(), ascending=False)


def _category_coverage(ic_df: pd.DataFrame, engine: FeatureEngine,
                       computed_set: set[str], status: dict[str, str]) -> pd.DataFrame:
    """每个类别里，有多少因子达到 engine 的 min_valid_ratio（不会被 _drop_dead_factors 丢弃）。

    区分三种状态：OK（有效） / ⚠整类缺失（测了但全废） / SKIP（默认未测，加 --include-extra）。
    """
    out = []
    for cat in CATEGORIES:
        cat_factors = registry.names(cat)
        if not cat_factors:
            continue
        computed = [f for f in cat_factors if f in computed_set]
        if not computed:
            all_extra = all(registry.meta(f).needs_extra for f in cat_factors)
            state = "SKIP（未测：--include-extra）" if all_extra else "—"
            out.append({
                "类别": cat,
                "因子总数": len(cat_factors),
                "有效因子数": 0,
                "缺失/未测": len(cat_factors),
                "状态": state,
            })
            continue
        sub = ic_df[ic_df["factor"].isin(computed)]
        alive = int((sub["覆盖率"] >= engine.min_valid_ratio).sum())
        total = len(sub)
        if alive == 0:
            miss = total
            # 若所有 Regime 下该类别权重均为 0 → 有意为之的"禁用"，不是退化
            cw = getattr(engine, "category_weights", {}) or {}
            all_zero = bool(cw) and all(
                abs(float(cw.get(rg, {}).get(cat, 0.0) or 0.0)) < 1e-9
                for rg in cw
            )
            state = "已禁用(权重0，非退化)" if all_zero else "⚠ 整类缺失→打分退化"
        else:
            miss = len(cat_factors) - alive
            state = "OK"
        out.append({
            "类别": cat,
            "因子总数": len(cat_factors),
            "有效因子数": alive,
            "缺失/未测": miss,
            "状态": state,
        })
    return pd.DataFrame(out)


def _regime_trajectory(hub, detector: RegimeDetector, panel: pd.DataFrame, start: date, end: date):
    """区间每天 Regime 分类计数（看是否长期黏在 TREND_UP）。"""
    idx = hub.get_index_bars("000300.SH", start, end)
    if idx is None or idx.empty:
        return None, "指数数据不可用"
    days = sorted(pd.to_datetime(idx["date"]).dt.date.unique())
    counts: dict[str, int] = {}
    for d in days:
        try:
            snap = detector.detect(d, panel=panel)
            counts[snap.regime.value] = counts.get(snap.regime.value, 0) + 1
        except Exception:  # noqa: BLE001
            counts["ERR"] = counts.get("ERR", 0) + 1
    return counts, f"共 {len(days)} 个交易日"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="因子 IC / 覆盖率 / Regime 诊断（防卡死版）")
    ap.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="结束日期，默认今天")
    ap.add_argument("--config", default=None, help="配置文件，默认 config/settings.yaml")
    ap.add_argument("--mode", default="paper", help="数据源模式 sim/paper/live，默认 paper")
    ap.add_argument("--warmup", type=int, default=250, help="因子预热天数")
    ap.add_argument("--periods", type=int, default=5, help="IC 前向收益天数")
    ap.add_argument("--max-symbols", type=int, default=None, help="限制标的数量加速（抽样仍具代表性）")
    ap.add_argument("--include-extra", action="store_true",
                    help="也尝试联网因子（基本面/资金流/新闻）；任一个卡住会超时跳过")
    ap.add_argument("--extra-timeout", type=int, default=DEFAULT_EXTRA_TIMEOUT,
                    help="联网因子单因子超时秒数（默认 90）")
    args = ap.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = Settings.load(args.config) if args.config else None
    with build_context(args.mode, settings=settings) as ctx:
        hub = ctx.hub
        engine = FeatureEngine(ctx.settings, hub)

        # 1) 全集
        infos = hub.get_instruments()
        universe = list(infos.keys()) if isinstance(infos, dict) else \
            [getattr(i, "symbol", str(i)) for i in (infos or [])]
        if args.max_symbols:
            universe = universe[: args.max_symbols]
        if not universe:
            raise SystemExit("标的和为空（数据源不可用？）")

        # 2) 原始面板（价格类因子全从这里算，不联网）
        panel = _load_panel(hub, universe, start, end, args.warmup)
        ctx_factor = FactorContext(asof=end, hub=hub, settings=ctx.settings)

        # 3) 因子级隔离计算：默认只算价格因子；--include-extra 才碰联网因子
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
            to = args.extra_timeout if is_extra else PRICE_TIMEOUT
            res = _compute_factor_safe(n, panel, ctx_factor, to)
            if isinstance(res, pd.Series):
                raw[n] = res
                status[n] = "OK"
            else:
                status[n] = res  # "TIMEOUT" / "ERROR:..."
                if res == "TIMEOUT":
                    had_timeout = True

        if not raw:
            raise SystemExit("没有任何因子算出有效值，无法诊断（检查行情面板）")
        raw_df = pd.DataFrame(raw, index=panel.index)
        panel = pd.concat([panel.reset_index(drop=True), raw_df.reset_index(drop=True)], axis=1)
        computed_set = set(raw.keys())

        # 4) 因子 IC 表
        ic_df = _factor_ic(panel, list(raw.keys()), args.periods)
        print("\n================ 因子 IC / IR（按 |IR| 降序）================")
        with pd.option_context("display.max_rows", 200, "display.width", 200):
            print(ic_df.to_string(index=False))

        # 5) 联网因子状态（直接暴露"是不是整路缺失"）
        if args.include_extra:
            print("\n================ 联网因子取数状态 ================")
            for n in sorted(extra_set):
                print(f"  {n:<24} {status.get(n, 'SKIP'):<28}"
                      f"{'  (needs网络)' if registry.meta(n).needs_extra else ''}")
        else:
            print("\n================ 联网因子 ================")
            print("  默认未测试（避免卡死）。加 --include-extra 可测，任一个卡住 90s 后自动跳过。")

        # 6) 类别覆盖率（验证"整类因子缺失"假设）
        cov = _category_coverage(ic_df, engine, computed_set, status)
        print("\n================ 因子类别覆盖率（是否被 _drop_dead_factors 丢弃）================")
        with pd.option_context("display.width", 220):
            print(cov.to_string(index=False))

        # 7) Regime 轨迹
        detector = RegimeDetector(ctx.settings, hub)
        counts, note = _regime_trajectory(hub, detector, panel, start, end)
        print("\n================ Regime 分类轨迹 ================")
        print(note)
        if counts:
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"  {k:<12} {v:>4} 天")

        # 8) 小结
        neg = ic_df[ic_df["IC"] < 0]
        print("\n================ 小结 ================")
        print(f"  已算因子 {len(ic_df)} 个，其中 IC<0（方向可能反/无 alpha）: {len(neg)} 个")
        dead = cov[(cov["有效因子数"] == 0) & (cov["状态"].str.contains("缺失|禁用", na=False))]
        if len(dead):
            degraded = dead[dead["状态"].str.contains("退化", na=False)]
            disabled = dead[dead["状态"].str.contains("禁用", na=False)]
            if len(degraded):
                print("  ⚠ 整类缺失(退化)类别:", ", ".join(degraded["类别"]),
                      "→ 重点排查其数据源（资金流/新闻多为 akshare 网络依赖）")
            if len(disabled):
                print(f"  ℹ 已禁用类别(权重0):", ", ".join(disabled["类别"]),
                      "→ 有意为之，无打分退化；换源恢复后需回灌权重")
        skipped = cov[cov["状态"].astype(str).str.startswith("SKIP")]
        if len(skipped):
            print("  ℹ 未测类别:", ", ".join(skipped["类别"]),
                  "→ 加 --include-extra 验证其数据源是否可达")
        if counts and counts.get("TREND_UP", 0) == (sum(counts.values()) if counts else 0):
            print("  ⚠ 全程 TREND_UP：策略在整个区间 80% 满仓，下跌市必亏，需收紧 Regime 阈值")

    # 仅当发生过超时（有残留网络线程未回收）才强制退出，避免进程卡在挂起线程上。
    if had_timeout:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
