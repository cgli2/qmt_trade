#!/usr/bin/env python
"""尾盘选股法 8 层筛选规则的「隔夜跳空预测力」检验（P1，2026-08-15）。

回答三个问题（把 V5/V6/V7 的"方向猜测"变成测量）：
1. 哪条筛选规则真的在挑「次日开盘高开」的票？
2. 整条漏斗最终选出的股票池，隔夜跳空是正还是负、低开率多高？
3. 大盘硬空仓闸门（沪深300 > MA60）到底过滤掉了什么？

方法
----
对区间内每个交易日 T，用 T 日收盘可得的日线特征（与 ``tail_pick.py screen()``
同口径，见下）把全市场分成「通过该规则 / 不通过」，比较两组的
**次日开盘跳空** gap = open(T+1)/close(T) − 1：

* 池化口径：全区间通过者均值 vs 全市场均值（简单平均，供参考）
* 每日口径：每天算 pass 均值 − 全市场均值，再对天数平均（消除单日系统性涨跌）
* 低开率：gap < −0.3%（V6 低开砍仓参考阈值）与 gap < −1%（深跳空）两档
* 正提升天数占比：pass 均值 > 全市场均值的交易日比例（方向稳定性）

同口径说明（镜像 tail_pick.py，防止"脚本与策略对不上"）：
* 涨幅 pct = close/prev_close − 1（prev_close 列缺失时回退上一交易日收盘）
* 量比 = 当日量 ÷ 前 5 日量均值（不含当日）
* 换手率 = volume / float_share（QMT 日线无 turnover_rate 列时的推算口径）
* 流通市值 = float_share × close
* 5 日超额 = (close/5 交易日前收盘 − 1) − 沪深300 同期涨幅
* MA20 = 含当日 20 日均线；硬排除 = 板块/ST/停牌/一字板/上市天数

用法
----
    # 全市场 2.5 年（约几分钟，取决于数据源与缓存）
    python scripts/tail_pick_rule_ic.py --start 2024-01-01 --end 2026-08-14

    # 抽样加速（1200 只仍有代表性）
    python scripts/tail_pick_rule_ic.py --start 2024-01-01 --end 2026-08-14 --max-symbols 1200

只读、只写 reports/，不修改 settings.yaml、不交易。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from qmt_trade.datahub.types import Adjust, Freq  # noqa: E402

logger = logging.getLogger("tailpick_ic")

#: V6 低开砍仓参考阈值（与 settings tail_pick.v6_low_cut_pct 同义，仅用于低开率统计）
GAP_CUT_PCT = 0.003
#: 深跳空参考阈值
GAP_DEEP_PCT = 0.01


# ====================================================================== 数据装配
def _load_panel(hub, universe, start: date, end: date, warmup_days: int = 120) -> pd.DataFrame:
    """日线长表（不复权，与执行口径一致）。"""
    fixed_start = start - timedelta(days=warmup_days)
    logger.info("取数 %s ~ %s（含 %d 天预热），标的 %d", fixed_start, end, warmup_days, len(universe))
    panel = hub.get_bars(list(universe), Freq.D1, fixed_start, end, Adjust.NONE, validate=True)
    if panel is None or panel.empty:
        raise SystemExit("行情面板为空（检查数据源）")
    panel = panel.copy()
    panel["date"] = pd.to_datetime(panel["date"])
    return panel.sort_values(["symbol", "date"]).reset_index(drop=True)


def _index_frame(hub, start: date, end: date, ma_days: int = 60) -> pd.DataFrame:
    """沪深300 日线：当日涨跌 + 5 日涨跌（规则⑦/⑨的基准）+ 自身 MA（大盘闸门）。

    注意：索引列一律加 ``idx_`` 前缀，避免与个股面板的 close/open 等列在 merge 时冲突。
    """
    idx = hub.get_index_bars("000300.SH", start - timedelta(days=120), end)
    if idx is None or idx.empty or "close" not in idx.columns:
        raise SystemExit("沪深300 指数数据不可用")
    df = idx.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["idx_close"] = df["close"].astype(float)
    df["idx_ret"] = df["idx_close"] / df["idx_close"].shift(1) - 1
    df["idx_ret5"] = df["idx_close"] / df["idx_close"].shift(5) - 1
    df["idx_ma"] = df["idx_close"].rolling(ma_days, min_periods=ma_days).mean()
    return df[["date", "idx_close", "idx_ret", "idx_ret5", "idx_ma"]]


def _static_flags(hub, symbols: list[str], allowed_boards: set[str]) -> pd.DataFrame:
    """每只标的的静态属性：is_st / float_share / list_date / 板块是否允许。"""
    from qmt_trade.core.instruments import detect_board, normalize_symbol

    infos = hub.get_instruments(symbols)
    items = list(infos.values()) if isinstance(infos, dict) else (infos or [])
    mp = {i.symbol: i for i in items}
    rows = []
    for s in symbols:
        i = mp.get(s)
        if i is None:
            rows.append({"symbol": s, "is_st": False, "float_share": 0.0,
                         "list_date": None, "board_ok": True})
            continue
        board = getattr(i, "board", None) or ""
        if not board:
            try:
                board = detect_board(normalize_symbol(s)).value
            except Exception:  # noqa: BLE001
                board = ""
        rows.append({
            "symbol": s,
            "is_st": bool(getattr(i, "is_st", False)),
            "float_share": float(getattr(i, "float_share", 0.0) or 0.0),
            "list_date": getattr(i, "list_date", None),
            "board_ok": (not board) or (board in allowed_boards),
        })
    return pd.DataFrame(rows)


# ====================================================================== 特征与规则
def _build_analysis(panel: pd.DataFrame, static: pd.DataFrame,
                    idx: pd.DataFrame, cfg) -> pd.DataFrame:
    """把日线长表扩展出全部规则布尔列与次日跳空标签（与 tail_pick.py 同口径）。"""
    g = panel.groupby("symbol", sort=False)
    df = panel.copy()
    # —— 基础特征 ——
    df["prev_close"] = df["prev_close"].where(df["prev_close"] > 0)
    df["prev_close"] = df["prev_close"].fillna(g["close"].shift(1))
    df["pct"] = df["close"] / df["prev_close"] - 1.0
    df["vol5_prev"] = g["volume"].shift(1).transform(
        lambda s: s.rolling(5, min_periods=5).mean())
    df["volume_ratio"] = df["volume"] / df["vol5_prev"]
    df["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["ret5"] = df["close"] / g["close"].shift(5) - 1.0
    df["yest_vol"] = g["volume"].shift(1)
    # —— 标签：次日开盘跳空（无未来函数：T 收盘后开 T+1 的仓，T+1 开盘价是标签）——
    df["gap"] = g["open"].shift(-1) / df["close"] - 1.0
    # —— 静态属性 / 指数（idx_* 前缀避免与个股 close 冲突）——
    df = df.merge(static, on="symbol", how="left")
    df["turnover"] = df["volume"] / df["float_share"].where(df["float_share"] > 0)
    df["float_mcap"] = df["float_share"] * df["close"]
    df = df.merge(idx, on="date", how="left")
    ld = df["list_date"].astype("datetime64[ns]")
    df["list_days_ok"] = (df["date"] - ld).dt.days >= cfg.min_list_days
    # —— 硬排除（镜像 TailPickScreener）——
    lu = df["limit_up"]
    df["limit_locked"] = lu.notna() & (lu > 0) & (df["close"] >= lu * 0.999)
    df["hard_ok"] = (df["board_ok"] & ~df["is_st"]
                     & ~df["is_suspended"].fillna(False)
                     & ~df["limit_locked"].fillna(False)
                     & df["list_days_ok"].fillna(False))
    # —— 规则（V5 当前配置语义）——
    df["r2"] = (df["pct"] >= cfg.min_pct_change) & (df["pct"] <= cfg.max_pct_change)
    df["r3"] = df["volume_ratio"] < cfg.shrink_volume_ratio_max
    df["r4"] = df["turnover"].between(cfg.min_turnover_rate, cfg.max_turnover_rate)
    df["r5"] = df["float_mcap"].between(cfg.min_float_market_cap, cfg.max_float_market_cap)
    df["r6a"] = df["volume"] >= df["yest_vol"] * cfg.volume_ladder_ratio
    df["r7"] = (df["pct"] - df["idx_ret"]) >= cfg.min_intraday_outperf_vs_index
    df["r9"] = (df["ret5"] - df["idx_ret5"]) >= cfg.min_5d_outperf_vs_index
    df["r13"] = df["close"] > df["ma20"]
    # 大盘硬空仓闸门（选项A：沪深300 > MA(market_ma_days) 才允许交易）——用指数自身均线
    df["gate_ok"] = df["idx_close"] > df["idx_ma"]
    return df


# ====================================================================== 统计
def _gap_stats(s: pd.Series) -> dict:
    s = s.dropna()
    if s.empty:
        return {"n": 0, "mean": np.nan, "median": np.nan,
                "gapdown_cut": np.nan, "gapdown_deep": np.nan}
    return {
        "n": int(len(s)),
        "mean": float(s.mean()),
        "median": float(s.median()),
        "gapdown_cut": float((s < -GAP_CUT_PCT).mean()),
        "gapdown_deep": float((s < -GAP_DEEP_PCT).mean()),
    }


def _rule_report(df: pd.DataFrame, rule_col: str, name: str, uni_col: str = "hard_ok") -> dict:
    """单规则：池化 + 每日口径 + 低开率 + 方向稳定天数。"""
    d = df[df[uni_col] & df["gap"].notna()].copy()
    if d.empty:
        return {"rule": name, "n": 0, "pass_mean": np.nan, "uni_mean": np.nan,
                "daily_lift": np.nan, "pos_days": np.nan, "n_days": 0,
                "pass_gapdown_cut": np.nan, "uni_gapdown_cut": np.nan}
    pass_ = d[d[rule_col]]
    ps = _gap_stats(pass_["gap"])
    us = _gap_stats(d["gap"])
    # 每日口径：每天 pass 均值 − 全市场均值
    lifts = []
    for _, day in d.groupby("date"):
        pp = day[day[rule_col]]["gap"].dropna()
        uu = day["gap"].dropna()
        if pp.empty or uu.empty:
            continue
        lifts.append(float(pp.mean()) - float(uu.mean()))
    daily_lift = float(np.mean(lifts)) if lifts else np.nan
    pos_days = float(np.mean([1.0 if x > 0 else 0.0 for x in lifts])) if lifts else np.nan
    return {
        "rule": name,
        "n": ps["n"],
        "pass_mean": ps["mean"],
        "uni_mean": us["mean"],
        "daily_lift": daily_lift,
        "pos_days": pos_days,
        "n_days": len(lifts),
        "pass_gapdown_cut": ps["gapdown_cut"],
        "uni_gapdown_cut": us["gapdown_cut"],
    }


_RULES = [
    ("r2", "② 涨幅带 [1%,3.5%]"),
    ("r3", "③ 缩量 <1.1×5日均量"),
    ("r4", "④ 换手率 [5%,10%]"),
    ("r5", "⑤ 流通市值 [50,500]亿"),
    ("r6a", "⑥a 今量≥昨量"),
    ("r7", "⑦ 当日跑赢沪深300"),
    ("r9", "⑨ 5日超额 ≥3%"),
    ("r13", "⑬ 站上MA20 (V7)"),
]

_FUNNEL = [
    ("r2", "② 涨幅带"),
    ("r3", "③ 缩量"),
    ("r4", "④ 换手率"),
    ("r5", "⑤ 流通市值"),
    ("r6a", "⑥a 阶梯放量"),
    ("r7", "⑦ 跑赢大盘"),
    ("r9", "⑨ 5日超额"),
    ("r13", "⑬ 站上MA20"),
]


def _funnel(df: pd.DataFrame, cols: list[tuple[str, str]]) -> list[dict]:
    """按规则顺序叠加（与筛选漏斗一致），看每一步对隔夜跳空的影响。"""
    out = []
    d = df[df["hard_ok"] & df["gap"].notna()].copy()
    d["pass"] = True
    out.append(_funnel_row("硬排除后全市场", d["pass"], d))
    for col, name in cols:
        d["pass"] = d["pass"] & d[col].fillna(False)
        out.append(_funnel_row(name, d["pass"], d))
    return out


def _funnel_row(name: str, mask: pd.Series, d: pd.DataFrame) -> dict:
    ps = _gap_stats(d.loc[mask, "gap"])
    return {"stage": name, "n": ps["n"], "mean": ps["mean"],
            "median": ps["median"], "gapdown_cut": ps["gapdown_cut"],
            "gapdown_deep": ps["gapdown_deep"]}


def _fmt(v, pct=False, digits=4) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    if pct:
        return f"{v * 100:.2f}%"
    return f"{v:.{digits}f}"


# ====================================================================== 主流程
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="尾盘选股 8 层规则隔夜跳空预测力检验（P1）")
    ap.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="结束日期，默认今天")
    ap.add_argument("--config", default=None, help="配置文件，默认 config/settings.yaml")
    ap.add_argument("--mode", default="paper", help="数据源模式 sim/paper/live，默认 paper")
    ap.add_argument("--max-symbols", type=int, default=None, help="抽样标的数加速")
    ap.add_argument("--out", default=None, help="报告输出路径，默认 reports/ 自动命名")
    args = ap.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from qmt_trade.app import build_context
    from qmt_trade.core.config import Settings
    from qmt_trade.strategies.tail_pick import TailPickConfig

    settings = Settings.load(args.config) if args.config else None
    with build_context(args.mode, settings=settings) as ctx:
        hub = ctx.hub
        cfg = TailPickConfig.from_settings(ctx.settings)

        infos = hub.get_instruments()
        universe = list(infos.keys()) if isinstance(infos, dict) else \
            [getattr(i, "symbol", str(i)) for i in (infos or [])]
        if args.max_symbols:
            universe = universe[: args.max_symbols]
        if not universe:
            raise SystemExit("标的和为空（数据源不可用？）")

        allowed = {str(b).upper() for b in (cfg.allowed_boards or ["MAIN", "GEM", "STAR"])}
        panel = _load_panel(hub, universe, start, end)
        static = _static_flags(hub, universe, allowed)
        idx = _index_frame(hub, start, end, ma_days=int(cfg.market_ma_days))
        df = _build_analysis(panel, static, idx, cfg)

        n_fs = int((static["float_share"] > 0).sum())
        logger.info("静态信息：float_share>0 覆盖 %d/%d（%.1f%%）", n_fs, len(static),
                    n_fs / len(static) * 100)

        # ---- 1) 全栈口径：漏斗 ----
        funnel = _funnel(df, _FUNNEL)
        # ---- 2) 单规则 ----
        reports = [_rule_report(df, c, n) for c, n in _RULES]
        # ---- 3) 涨幅带敏感性 ----
        d = df[df["hard_ok"] & df["gap"].notna()].copy()
        bands = [(0.0, 0.01, "0~1%"), (0.01, 0.02, "1~2%"), (0.02, 0.03, "2~3%"),
                 (0.03, 0.035, "3~3.5%"), (0.035, 0.05, "3.5~5%")]
        band_rows = []
        for lo, hi, label in bands:
            m = d["pct"].between(lo, hi)
            band_rows.append({"band": label, **{k: v for k, v in
                                                _gap_stats(d.loc[m, "gap"]).items()}})
        # ---- 4) 大盘闸门 ----
        gd = df[df["hard_ok"] & df["gap"].notna()].copy()
        gate_on = _gap_stats(gd.loc[gd["gate_ok"], "gap"])
        gate_off = _gap_stats(gd.loc[~gd["gate_ok"], "gap"])

        # ---- 输出 ----
        lines = [f"# 尾盘选股 8 层规则 · 隔夜跳空预测力（{start} ~ {end}）",
                 "",
                 f"- 标的：{len(universe)}（{'抽样' if args.max_symbols else '全市场'}）"
                 f" ｜ float_share 覆盖 {n_fs}/{len(static)}（{n_fs / len(static):.1%}）",
                 f"- 规则参数：V5 当前配置（涨幅带 [{cfg.min_pct_change},{cfg.max_pct_change}]、"
                 f"缩量 <{cfg.shrink_volume_ratio_max}×、换手 "
                 f"[{cfg.min_turnover_rate},{cfg.max_turnover_rate}]、市值 "
                 f"[{cfg.min_float_market_cap / 1e8:.0f},{cfg.max_float_market_cap / 1e8:.0f}]亿、"
                 f"5日超额 ≥{cfg.min_5d_outperf_vs_index:.0%}）",
                 f"- 口径：gap = 次日开盘/当日收盘 − 1；低开率阈值 −0.3% / −1.0%",
                 "",
                 "## 一、整条漏斗的隔夜跳空（核心）",
                 "",
                 "| 阶段 | 样本 | 均值 gap | 中位 gap | 低开率<−0.3% | 低开率<−1% |",
                 "|---|---:|---:|---:|---:|---:|"]
        for r in funnel:
            lines.append(f"| {r['stage']} | {r['n']:,} | {_fmt(r['mean'], pct=True)} | "
                         f"{_fmt(r['median'], pct=True)} | {_fmt(r['gapdown_cut'], pct=True)} | "
                         f"{_fmt(r['gapdown_deep'], pct=True)} |")
        lines += ["", "## 二、单规则对次日开盘跳空的预测力", "",
                  "| 规则 | 通过样本 | 通过均值gap | 全市场均值gap | 每日提升 | 提升为正天数占比 | 通过低开率 | 全市场低开率 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for r in reports:
            lines.append(f"| {r['rule']} | {r['n']:,} | {_fmt(r['pass_mean'], pct=True)} | "
                         f"{_fmt(r['uni_mean'], pct=True)} | {_fmt(r['daily_lift'], pct=True)} | "
                         f"{_fmt(r['pos_days'], pct=True)} | {_fmt(r['pass_gapdown_cut'], pct=True)} | "
                         f"{_fmt(r['uni_gapdown_cut'], pct=True)} |")
        lines += ["", "## 三、涨幅带内部分布（规则②敏感性）", "",
                  "| 当日涨幅带 | 样本 | 均值 gap | 中位 gap | 低开率<−0.3% |",
                  "|---|---:|---:|---:|---:|"]
        for r in band_rows:
            lines.append(f"| {r['band']} | {r['n']:,} | {_fmt(r['mean'], pct=True)} | "
                         f"{_fmt(r['median'], pct=True)} | {_fmt(r['gapdown_cut'], pct=True)} |")
        lines += ["", "## 四、大盘硬空仓闸门（沪深300 > MA%d）" % cfg.market_ma_days, "",
                  "| 闸门状态 | 样本 | 均值 gap | 低开率<−0.3% |",
                  "|---|---:|---:|---:|",
                  f"| 放行日（站上 MA{cfg.market_ma_days}） | {gate_on['n']:,} | "
                  f"{_fmt(gate_on['mean'], pct=True)} | {_fmt(gate_on['gapdown_cut'], pct=True)} |",
                  f"| 拦截日（跌破 MA{cfg.market_ma_days}） | {gate_off['n']:,} | "
                  f"{_fmt(gate_off['mean'], pct=True)} | {_fmt(gate_off['gapdown_cut'], pct=True)} |"]
        report = "\n".join(lines)

        out_path = Path(args.out) if args.out else \
            _ROOT / "reports" / f"tail_pick_rule_ic_{start}_{end}.md"
        out_path.write_text(report + "\n", encoding="utf-8")

        print("\n" + "=" * 78)
        print(f"尾盘选股规则隔夜跳空检验 {start} ~ {end}（标的 {len(universe)}）")
        print("=" * 78)
        print(report)
        print(f"\n已写入：{out_path}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
