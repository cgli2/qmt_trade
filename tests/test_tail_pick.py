"""尾盘选股法（独立策略）逻辑单测 + SIM 机制自检。

设计目标（2026-08-12）
-------------------
不依赖任何真实数据源：用一个确定性的 **合成内存 DataHub**（FakeHub）造出
分钟级行情，验证：
  1. 8 层筛选规则各自正确（涨幅/量比/换手/流通市值/阶梯放量/尾盘筹码结构/分时跑赢大盘）；
  2. sim（无分钟线）降级路径：分钟规则 best-effort 放行、策略仍跑通；
  3. 一夜持股买卖机制：T 日 14:30 买入、T+1 09:30~10:30 时间不对称离场、持仓 1 日；
  4. 低开保护：T+1 深低开等 9:45，未翻红砍仓（TAIL_LOWOPEN_CUT）、亏损落地；
  5. 成本模型（佣金/印花税/滑点）已计入成交；
  6. ``run_selfcheck()`` 供 CLI ``tailpick validate`` 调用，给出机制自检结论。

⚠ 这些测试用的是**合成行情**，只验证"策略逻辑与撮合机制正确"，**不代表真实 A 股业绩**。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

# 让本文件可直接以脚本方式运行（pytest 也会加到 path）
sys_path = sys.path
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from qmt_trade.datahub.types import Adjust, Freq, InstrumentInfo  # noqa: E402
from qmt_trade.strategies.tail_pick import (  # noqa: E402
    TailPickBacktester, TailPickConfig, TailPickScreener, TailPickStrategy,
)

_DAYS = [date(2026, 7, d) for d in range(27, 32)] + \
        [date(2026, 8, d) for d in range(1, 8)]   # 07-27 .. 08-07


class FakeHub:
    """确定性合成 DataHub：日线 + 分钟线 + 标的信息 + 指数日线。"""

    def __init__(self, daily: pd.DataFrame, minute: pd.DataFrame,
                 instruments: dict, index_daily: pd.DataFrame):
        self.daily = daily
        self.minute = minute
        self.instruments = instruments
        self.index_daily = index_daily

    def get_bars(self, symbols, freq=Freq.D1, start=None, end=None, adjust=Adjust.NONE,
                 validate=True):
        if isinstance(symbols, str):
            symbols = [symbols]
        frame = self.daily if freq == Freq.D1 else self.minute
        df = frame.copy()
        if symbols is not None:
            df = df[df["symbol"].isin(set(symbols))]
        # 按"自然日"过滤：日线 date 为 0 点；分钟线带时分秒，
        # 必须用 .dt.date 比对，否则分钟线会被 end=当天 0 点全部滤掉。
        if start is not None:
            sd = pd.Timestamp(start).date()
            df = df[pd.to_datetime(df["date"]).dt.date >= sd]
        if end is not None:
            ed = pd.Timestamp(end).date()
            df = df[pd.to_datetime(df["date"]).dt.date <= ed]
        return df.reset_index(drop=True)

    def get_instruments(self, symbols=None):
        if symbols is None:
            return list(self.instruments.values())
        return [i for i in self.instruments.values() if i.symbol in set(symbols)]

    def get_index_bars(self, symbol, start=None, end=None):
        df = self.index_daily.copy()
        df = df[df["symbol"] == symbol]
        if start is not None:
            df = df[pd.to_datetime(df["date"]) >= pd.Timestamp(start)]
        if end is not None:
            df = df[pd.to_datetime(df["date"]) <= pd.Timestamp(end)]
        return df.reset_index(drop=True)


# ----------------------------------------------------------------- 数据生成
def _daily_rows(sym, days, close0, growth, vol_base=1_000_000, vol_growth=1.4,
                turnover=0.07, float_share=1_000_000_000):
    rows, prev = [], None
    for i, d in enumerate(days):
        close = round(close0 * (growth ** i), 4)
        vol = int(vol_base * (vol_growth ** i))
        prev_close = round(close / growth, 4) if prev is None else prev
        high = round(close * 1.004, 4)
        low = round(close * 0.996, 4)
        open_ = round(prev_close, 4) if prev_close else round(close * 0.999, 4)
        rows.append(dict(date=pd.Timestamp(d), symbol=sym, open=open_, high=high, low=low,
                         close=close, volume=vol, amount=vol * close, turnover_rate=turnover,
                         prev_close=prev_close, limit_up=round(close * 1.1, 4),
                         limit_down=round(close * 0.9, 4)))
        prev = close
    return rows


def _minute_rows(sym, d, close, day_high, valid, base_vol=100.0, tail_vol=200.0):
    """生成 A 股真实交易时段分钟线：上午 09:35~11:30 + 下午 13:05~15:00（共 48 根 5min）。

    与 QMT 一致按【结束时刻】标记（首根 09:35 覆盖 09:30~09:35、末根 15:00），
    覆盖 14:25~15:00 尾盘窗口，否则规则⑥b/⑧' 的尾盘切片会落在空集。
    规则⑧'筹码结构要求尾盘收阳（tail_close > tail_open）且收盘 < 全天 VWAP：
    valid 时尾盘 bar 低开收阳、成交额加倍抬高 VWAP；invalid（C）收阴失败。
    """
    rows = []

    def _gen(start_hour, start_min, n):
        t0 = datetime(d.year, d.month, d.day, start_hour, start_min)
        for i in range(n):
            ts = t0 + timedelta(minutes=5 * i)
            vol = tail_vol if ts.time() >= time(14, 30) else base_vol
            tail = ts.time() >= time(14, 25)
            if valid and tail:
                o, h, l, c = close * 0.997, day_high, close * 0.997, close
                amount = vol * close * 2.0      # 抬全天 VWAP → 收盘 < VWAP
            elif valid:
                o, h, l, c = close, day_high, close, close
                amount = vol * close
            else:
                o, h, l, c = close * 1.002, round(close * 1.002, 4), close, close
                amount = vol * close
            rows.append(dict(date=ts, symbol=sym, open=round(o, 4), high=round(h, 4),
                             low=round(l, 4), close=round(c, 4), volume=vol,
                             amount=amount))

    _gen(9, 35, 24)   # 09:35 ~ 11:30（结束时刻标记，首根覆盖 09:30~09:35）
    _gen(13, 5, 24)   # 13:05 ~ 15:00
    return rows


def _index_rows(days, close0=4000.0):
    return [dict(date=pd.Timestamp(d), symbol="000300.SH", open=close0, high=close0,
                 low=close0, close=close0, volume=1, amount=close0) for d in days]


def build_hub(scenario: str = "full") -> FakeHub:
    """scenario: 'full'（A/D 通过、B 跌出涨幅、C 尾盘新高失败）/ 'stop'（次日跳空）/ 'no_minute'（无分钟线）。"""
    daily = []
    minute = []
    inst: dict[str, InstrumentInfo] = {}

    # A: 通过（日涨 3%，流通市值≈123亿，分钟合法）      主板
    # B: 失败（日涨 6% 超出 V5 统一带上界 3.5%）       主板
    # C: 日线通过但分钟尾盘收阴（规则⑧'筹码失败）      主板
    # D: 通过（日涨 2.5%，GEM，分钟合法）              创业板
    # （V5 统一涨幅带 1%~3.5%，增速须落在带内且远离边界避免 round(4) 舍入踢出）
    specs = {
        "600001.SH": dict(close0=10.00, growth=1.03, float_share=1_000_000_000, valid=True),
        "600002.SH": dict(close0=10.00, growth=1.06, float_share=1_000_000_000, valid=True),
        "600003.SH": dict(close0=10.00, growth=1.03, float_share=1_000_000_000, valid=False),
        "300001.SZ": dict(close0=10.00, growth=1.025, float_share=800_000_000, valid=True),
    }
    for sym, sp in specs.items():
        daily.extend(_daily_rows(sym, _DAYS, sp["close0"], sp["growth"]))
        if scenario != "no_minute":
            for d in _DAYS:
                close = round(sp["close0"] * (sp["growth"] ** _DAYS.index(d)), 4)
                day_high = round(close * 1.004, 4)
                minute.extend(_minute_rows(sym, d, close, day_high, sp["valid"]))
        inst[sym] = InstrumentInfo(
            symbol=sym, name=sym, industry="测试", list_date=date(2020, 1, 1),
            total_share=sp["float_share"] * 2, float_share=sp["float_share"],
            is_st=False, is_suspended=False, market_cap=sp["float_share"] * sp["close0"] * 2,
        )

    if scenario == "stop":
        # 仅 A：构造次日深低开（08-04 开盘从 12.30 直接跳到 10.00）。
        # 关键：分钟线必须与该日"实际"日线 close/high 一致，否则筹码结构规则⑧' 会对不上。
        # growth 须落在 V5 统一涨幅带 1%~3.5% 内且远离上界（避免 round(4) 舍入踢出）。
        daily = _daily_rows("600001.SH", _DAYS, 10.00, 1.03)
        # 覆盖 08-04 日线：跳空低开
        for r in daily:
            if r["date"] == pd.Timestamp(date(2026, 8, 4)):
                r["open"] = 10.00
                r["high"] = 10.10
                r["low"] = 9.95
                r["close"] = 10.05
                r["prev_close"] = 10.40   # 与 08-03 真实收盘(12.30)不一致也无妨：次日仅卖出不参与选股
        # 用"每日日线真实 close/high"重建分钟线，保证 08-03 选股日规则⑥⑧ 自洽
        minute = []
        for r in daily:
            d = r["date"].date()
            minute.extend(_minute_rows("600001.SH", d, r["close"], r["high"], True))
        # 覆盖 08-04 09:35 那根分钟开盘为跳空价 10.00（低开分支判定点，结束时刻标记）
        for r in minute:
            if r["date"] == pd.Timestamp(datetime(2026, 8, 4, 9, 35)):
                r["open"] = 10.00
                r["high"] = 10.10
                r["low"] = 9.95
                r["close"] = 10.02
        inst = {"600001.SH": InstrumentInfo(
            symbol="600001.SH", name="A", industry="测试", list_date=date(2020, 1, 1),
            total_share=2_000_000_000, float_share=1_000_000_000,
            is_st=False, is_suspended=False, market_cap=20_000_000_000)}

    index = _index_rows(_DAYS)
    return FakeHub(pd.DataFrame(daily), pd.DataFrame(minute), inst, pd.DataFrame(index))


def _instr_map(hub):
    return {i.symbol: i for i in hub.get_instruments()}


def _base_cfg(**kw) -> TailPickConfig:
    """测试基线配置：合成市场仅 4 只股票，需放宽广度阈值，
    否则强市广度闸门（2026-08-13 改进二）会拦截所有基线用例。"""
    kw.setdefault("weak_market_adv_min", 3)
    kw.setdefault("breadth_block_below", 1)
    # 合成市场量比≈2.46，低于生产默认 2.5 → 基线用例放宽量比门槛
    kw.setdefault("min_volume_ratio", 1.0)
    # V4.0：合成量比≈2.46 ≥ 默认异常放量剔除 2.0 → 基线用例放宽，避免全局剔除误杀
    kw.setdefault("vol_spike_exclude_ratio", 3.0)
    # V5：合成量比≈2.46 ≥ 默认缩量上限 1.1 → 基线用例放宽，避免缩量规则全杀
    kw.setdefault("shrink_volume_ratio_max", 3.0)
    # V6 低位防御默认开启；既有 V4/V5 回归用例显式关闭（V6 用例自行显式开启）
    kw.setdefault("defense_mode", False)
    # 默认不加载真实行业映射，避免板块过滤干扰合成用例
    kw.setdefault("industry_map_path", "")
    return TailPickConfig(**kw)


# ----------------------------------------------------------------- 规则单测
def test_rule_pct_and_minute_exclusion():
    hub = build_hub("full")
    cfg = _base_cfg()
    scr = TailPickScreener(cfg)
    day = date(2026, 8, 5)
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub), minute_available=True)
    syms = {p.symbol for p in picks}
    assert "600001.SH" in syms, "A 应通过 8 层筛选"
    assert "300001.SZ" in syms, "D（创业板）应通过 8 层筛选"
    assert "600002.SH" not in syms, "B 涨幅 6% 应被规则②剔除"
    assert "600003.SH" not in syms, "C 尾盘收阴（筹码结构失败）应被规则⑧'剔除（严格分钟模式）"
    # 通过项应标记分钟已验证
    assert all(p.minute_verified for p in picks), "full 场景下分钟规则应严格验证"


def test_rule_best_effort_no_minute():
    hub = build_hub("no_minute")
    cfg = _base_cfg()
    scr = TailPickScreener(cfg)
    day = date(2026, 8, 5)
    # sim 模式：无分钟线，分钟规则 best-effort，C 也应放行（仅验证日线规则）
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub), minute_available=False)
    syms = {p.symbol for p in picks}
    assert "600001.SH" in syms
    assert "600003.SH" in syms, "无分钟线时 C 应 best-effort 放行（仅日线规则生效）"
    assert all(not p.minute_verified for p in picks), "无分钟线时 minute_verified 应为 False"


def test_rule_index_outperformance_strict():
    """规则⑦（跑赢大盘，日线口径）：个股当日涨幅不及沪深300 当日涨幅 → 剔除。"""
    hub = build_hub("full")
    cfg = _base_cfg()
    scr = TailPickScreener(cfg)
    day = date(2026, 8, 5)
    # 指数日线其余日均恒定 4000 → 当日指数大涨 +5%，个股仅 +3~4% → 跑输
    mask = pd.to_datetime(hub.index_daily["date"]).dt.date == day
    hub.index_daily.loc[mask, "close"] = 4200.0
    # （个股涨幅 3.2~3.5% 仍在生产涨幅带内，仅因跑输指数被规则⑦剔除）
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub),
                       minute_available=True)
    assert not picks, "个股涨幅 3~4% < 指数 +5% → 应全部被规则⑦剔除"


def test_rule_daily_volume_ladder():
    """规则⑥a（阶梯放量·日线层）：今日量未达 昨日量 × ratio → 剔除。

    V5 缩量哲学下 ⑥a 已停用（strong_trend_mode=True），仅 V4 回退路径生效，
    故本用例显式 strong_trend_mode=False 验证原语义。"""
    hub = build_hub("full")
    cfg = _base_cfg(volume_ladder_ratio=1.5, strong_trend_mode=False)
    scr = TailPickScreener(cfg)
    day = date(2026, 8, 5)
    # 合成数据今/昨量比恒为 1.4 < 1.5 → 全部剔除（与量比规则隔离：量比仍满足 >1）
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub),
                       minute_available=True)
    assert not picks, "今/昨量 1.4 < 1.5 → 应全部被规则⑥a 剔除（V4 回退路径）"


def test_rule_intraday_volume_ladder():
    """规则⑥b（阶梯放量·分时层）：午后等分 3 段、各段量逐段递增。

    合成数据午后 24 根（13:05~15:00，结束时刻标记）均匀分入 3 段（各 8 根），
    前两段量 800、末段（14:30~15:00 共 7 根量 200 + 14:25 一根量 100）量 1500 → 不递减，
    正常应通过；把 14:30 后量调小使末段缩量 → 严格模式剔除。
    """
    hub = build_hub("full")
    cfg = _base_cfg()
    scr = TailPickScreener(cfg)
    day = date(2026, 8, 5)
    # 边界断言：分段分布与设计一致，未缩量时 ⑥b 应通过（A/D 在选）
    vols = scr._ladder_seg_volumes(hub.minute, "600001.SH", day)
    assert vols == [800.0, 800.0, 1500.0], f"午后 3 段量应为 800/800/1500，实得 {vols}"
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub),
                       minute_available=True)
    assert {p.symbol for p in picks} == {"600001.SH", "300001.SZ"}, \
        "未缩量时 A/D 应通过 ⑥b 入选"
    # 把 14:30 后的分钟量调小（最后一段量小于前段）→ 分段不递增
    dt = pd.to_datetime(hub.minute["date"])
    mask = (dt.dt.date == day) & (dt.dt.time >= time(14, 30))
    hub.minute.loc[mask, "volume"] = 50.0
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub),
                       minute_available=True)
    assert not picks, "尾段缩量 → 应全部被规则⑥b 剔除（严格分钟模式）"


def test_rule_chip_structure():
    """规则⑧'（筹码结构，2026-08-13 四段式革命取代原尾盘新高）：
    尾盘收阳且收盘 < 全天 VWAP 通过；收盘高于 VWAP → 严格模式剔除。"""
    hub = build_hub("full")
    cfg = _base_cfg()
    scr = TailPickScreener(cfg)
    day = date(2026, 8, 5)
    # 基线：合成尾盘阳线 + 成交额加倍抬 VWAP（≈1.27×close）→ A/D 筹码结构通过
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub),
                       minute_available=True)
    assert {p.symbol for p in picks} == {"600001.SH", "300001.SZ"}, \
        "收阳且低于 VWAP 应通过筹码结构过滤"
    # 缩小分钟成交额 → 全天 VWAP≈0.76×close 跌破收盘 → 筹码结构失败
    dt = pd.to_datetime(hub.minute["date"])
    mask = dt.dt.date == day
    hub.minute.loc[mask, "amount"] *= 0.6
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub),
                       minute_available=True)
    assert not picks, "收盘高于 VWAP → 应全部被规则⑧'剔除（严格分钟模式）"


# ----------------------------------------------------------------- 回测机制
def test_backtester_roundtrip_one_night():
    hub = build_hub("full")
    cfg = _base_cfg()
    bt = TailPickBacktester(__fake_settings(), hub, initial_cash=1_000_000, config=cfg,
                            require_minute=None)
    res = bt.run(date(2026, 8, 3), date(2026, 8, 7))
    assert res.metrics is not None, "应产出指标"
    assert res.minute_available is True, "full 场景应有分钟线"
    assert len(res.equity_curve) == 5, f"权益曲线应 5 点，实得 {len(res.equity_curve)}"
    assert all(np.isfinite(res.equity_curve)), "权益曲线不应含 NaN"
    assert len(res.closed_trades) >= 1, "至少应有 1 笔平仓"
    # 一夜持股：平仓持有天数应为 1
    assert all(int(t["holding_days"]) == 1 for t in res.closed_trades), \
        f"持仓天数应全为 1，实得 {[t['holding_days'] for t in res.closed_trades]}"
    # 成本已计入：取一笔平仓看 pnl 含费用
    assert res.metrics.get("total_return") is not None


def test_backtester_cost_and_gap_attribution():
    """P0（2026-08-15）：回测应产出成本归因与离场信号归因，
    且满足恒等式 净盈亏 = 信号毛盈亏 − 成本拖累（滑点 + 显式费用）。"""
    hub = build_hub("full")
    cfg = _base_cfg()
    bt = TailPickBacktester(__fake_settings(), hub, initial_cash=1_000_000, config=cfg,
                            require_minute=None)
    res = bt.run(date(2026, 8, 3), date(2026, 8, 7))
    ca = res.cost_attribution
    assert ca, "应产出成本归因"
    assert ca["n_buys"] > 0 and ca["n_sells"] > 0, "应有买卖成交"
    assert ca["cost_drag"] > 0, "成本拖累应 > 0（FIXED 滑点 0.2%×2 + 费用）"
    assert ca["slippage"] > 0 and ca["explicit_fees"] > 0
    assert abs(ca["gross_pnl"] - (ca["net_pnl"] + ca["cost_drag"])) < 1e-6, \
        "恒等式：净盈亏 = 信号毛盈亏 − 成本拖累"
    assert ca["single_side_turnover"] > 0 and ca["roundtrip_cost_rate"] > 0
    assert 1 <= ca["n_round_trips"] <= ca["n_sells"], \
        "合并拆笔后的真实笔数应 ≤ 离场记录数"
    ga = res.gap_attribution
    assert ga, "应产出离场信号归因"
    assert ga[-1]["reason"] == "ALL", "末行应为 ALL 合计"
    assert sum(r["n"] for r in ga[:-1]) == ga[-1]["n"], "分信号笔数之和应等于 ALL 行"


def test_overnight_lowopen_cut():
    """低开分支（2026-08-13 四段式革命）：深低开竞价不卖，等 09:45 bar——
    期间最高价未翻红（未 > T日收盘）→ 09:45 收盘市价砍仓（TAIL_LOWOPEN_CUT）。
    翻红变体：期间曾 > 昨收 → 不砍仓，转正常离场链。"""
    hub = build_hub("stop")
    cfg = _base_cfg(weak_market_adv_min=0)  # stop 市场仅 1 家上涨，放宽广度避免闸门拦截
    bt = TailPickBacktester(__fake_settings(), hub, initial_cash=1_000_000, config=cfg,
                            require_minute=None)
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    assert res.metrics is not None
    cuts = [t for t in res.closed_trades if t.get("reason") == "TAIL_LOWOPEN_CUT"]
    assert cuts, f"深低开未翻红应 9:45 砍仓，closed_trades={res.closed_trades}"
    assert all(float(t["pnl"]) < 0 for t in cuts), "低开砍仓应为亏损落地"
    # 成交含费用（佣金/印花税/滑点）
    fill = res.trades[0]
    assert fill.total_fee > 0, "成交应计入费用"
    # 翻红变体：09:40 bar 最高 12.35 > 昨收 12.2987（stop 场景 growth=1.03）→ 保留持仓走正常离场链
    hub2 = build_hub("stop")
    _override_bars(hub2, date(2026, 8, 4), {time(9, 40): {"high": 12.35}})
    bt2 = TailPickBacktester(__fake_settings(), hub2, initial_cash=1_000_000, config=cfg,
                             require_minute=None)
    res2 = bt2.run(date(2026, 8, 3), date(2026, 8, 4))
    assert not any(t.get("reason") == "TAIL_LOWOPEN_CUT" for t in res2.closed_trades), \
        "翻红后不应再砍仓"
    assert any(t["symbol"] == "600001.SH" for t in res2.closed_trades), \
        "翻红保留后仍应在离场窗口内平仓"


# ----------------------------------------------------------------- 离场增强（2026-08-13）
def _override_bars(hub: "FakeHub", day: date, overrides: dict[time, dict]):
    """按时刻覆写某日分钟线 OHLCV（构造特定离场路径）。"""
    dt = pd.to_datetime(hub.minute["date"])
    for tm, vals in overrides.items():
        mask = (dt.dt.date == day) & (dt.dt.time == tm)
        for col, v in vals.items():
            hub.minute.loc[mask, col] = v


def _exit_reasons(start: date = date(2026, 8, 3), end: date = date(2026, 8, 4),
                  cfg: TailPickConfig | None = None):
    hub = build_hub("full")
    return hub, TailPickBacktester(__fake_settings(), hub, initial_cash=1_000_000,
                                   config=cfg or _base_cfg(), require_minute=None)


def test_exit_gap_protect_flat_open():
    """② 缺口保护·深平/低开：T+1 低开超过缓冲幅度（0.5%）→ 开盘价即走（TAIL_GAPSTOP）。

    合成数据 08-03 收盘 12.2987（即 T+1 的昨收/last_price）；把 08-04 首根 bar
    整体平移到 12.25（低于昨收且低于缓冲下限 ≈ 12.26）即触发。"""
    hub, bt = _exit_reasons()
    _override_bars(hub, date(2026, 8, 4), {
        time(9, 35): {"open": 12.25, "high": 12.25, "low": 12.25, "close": 12.25,
                      "amount": 100.0 * 12.25}})
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600001.SH"]
    assert [t["reason"] for t in got] == ["TAIL_GAPSTOP"], \
        f"深低开应触发缺口保护，实得 {got}"


def test_exit_gap_buffer_recover():
    """② 缺口保护·5分钟缓冲（反抽路径）：平开/微低开（成本 ±0.5% 内）不立即卖，
    首根 09:35 bar 盘中最高触及成本 → 保本即走（TAIL_GAPRECOV）。

    成本≈ 12.32，缓冲下限≈ 12.26；open 设 12.27（≤ 昨收 12.2987 且 ≥ 下限），
    首根 high 12.34 ≥ 成本 → 按成本价限价成交。"""
    hub, bt = _exit_reasons()
    _override_bars(hub, date(2026, 8, 4), {
        time(9, 35): {"open": 12.27, "high": 12.34, "low": 12.24, "close": 12.29,
                      "amount": 100.0 * 12.29}})
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600001.SH"]
    assert len(got) == 1
    t = got[0]
    assert t["reason"] == "TAIL_GAPRECOV", f"反抽触及成本应保本即走，实得 {t}"
    assert t["exit_price"] == pytest.approx(
        round(round(t["entry_price"], 4) * 0.998, 4), rel=1e-6), \
        "反抽出局成交价应为 round(成本)×(1-滑点)"


def test_exit_gap_buffer_no_recover():
    """② 缺口保护·5分钟缓冲（未反抽路径）：缓冲期内未摸到成本 →
    9:35 按首根 bar 收盘离场（TAIL_GAPWAIT，回测口径等价 9:35 市价）。"""
    hub, bt = _exit_reasons()
    _override_bars(hub, date(2026, 8, 4), {
        time(9, 35): {"open": 12.27, "high": 12.31, "low": 12.20, "close": 12.22,
                      "amount": 100.0 * 12.22}})
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600001.SH"]
    assert len(got) == 1
    t = got[0]
    assert t["reason"] == "TAIL_GAPWAIT", f"缓冲未反抽应 9:35 离场，实得 {t}"
    assert t["exit_price"] == pytest.approx(
        round(round(12.22, 4) * 0.998, 4), rel=1e-6), \
        "缓冲未反抽应按首根 bar 收盘×(1-滑点) 离场"
    assert float(t["pnl"]) < 0


def test_exit_take_profit():
    """高开 > +0.5% → 竞价卖半仓（TAIL_HALF_OPEN）；剩余半仓首根 bar 冲高触及
    成本×1.028 → 止盈落袋（TAIL_TP）。首根量未达隔夜动量阈值（vol=100 < 2×bar均量）。

    昨收 12.2987 → open 12.42（+0.99% 高开）；成本≈ 12.32 → 止盈线≈ 12.67
    （V4.0：take_profit_pct 1.8%→2.8%）。"""
    hub, bt = _exit_reasons()
    _override_bars(hub, date(2026, 8, 4), {
        time(9, 35): {"open": 12.42, "high": 12.70, "low": 12.42, "close": 12.64,
                      "amount": 100.0 * 12.64}})
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600001.SH"]
    assert [t["reason"] for t in got] == ["TAIL_HALF_OPEN", "TAIL_TP"], \
        f"高开应先卖半仓再止盈，实得 {got}"
    # 首笔为半仓：奇数总股数时半仓向下取整，剩仓最多比半仓多 100 股
    total = sum(t["shares"] for t in got)
    assert 0 <= total - got[0]["shares"] * 2 <= 100, \
        f"首笔应为半仓，半仓={got[0]['shares']} 总股数={total}"
    tp = got[1]
    # 限价单按止盈价成交再扣滑点（挂单价先 round 4 位，卖出滑点向下 0.2%）
    assert tp["exit_price"] == pytest.approx(
        round(round(tp["entry_price"] * 1.028, 4) * 0.998, 4), rel=1e-6), \
        "止盈成交价应为 round(成本×(1+take_profit_pct))×(1-滑点)"
    assert all(float(t["pnl"]) > 0 for t in got), "高开半仓+止盈均应盈利"


def test_exit_breakeven():
    """③ 保本单：开盘 5min 触及 成本×1.010 激活保本 → 回落触及成本价成交（TAIL_BREAKEVEN）。

    成本≈ 12.32：保本触发线≈ 12.45，止盈线≈ 12.67（首根 high 控在两者之间）；
    open 12.34 微高开（+0.34% < 0.5%）属平开分支。"""
    hub, bt = _exit_reasons()
    _override_bars(hub, date(2026, 8, 4), {
        # 首根：冲高激活保本（high ≥ 1.010 线）但不触发保本/止盈，amount 同步 close 避免误触 VWAP
        time(9, 35): {"open": 12.34, "high": 12.46, "low": 12.34, "close": 12.40,
                      "amount": 100.0 * 12.40},
        # 次根：回落触及成本 → 保本单成交
        time(9, 40): {"open": 12.40, "high": 12.40, "low": 12.30, "close": 12.31,
                      "amount": 100.0 * 12.31},
    })
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600001.SH"]
    assert len(got) == 1
    t = got[0]
    assert t["reason"] == "TAIL_BREAKEVEN", f"回落应触发保本单，实得 {t}"
    # 限价单按成本价成交再扣滑点（成交价略低于成本，但亏损仅为费用级，保本语义不变）
    assert t["exit_price"] == pytest.approx(
        round(round(t["entry_price"], 4) * 0.998, 4), rel=1e-6), \
        "保本成交价应为 round(成本)×(1-滑点)"


def test_exit_vwap_stop():
    """⑤ VWAP 止损：未止盈且 bar 收盘跌破当日分时均价 → 市价离场（TAIL_VWAP_EXIT）。

    首根平走 12.31（低于保本触发线 12.45，不激活保本；close == VWAP 严格小于不触发），
    次根跌破均价（累计 VWAP 12.215 > close 12.12）。

    V4.0：VWAP 止损默认关闭（让位 -1.2% 硬止损）→ 本用例显式开启，并放宽硬止损
    至 3%（止损线≈ 11.95）避免先于 VWAP 触发。"""
    hub, bt = _exit_reasons(cfg=_base_cfg(vwap_exit_enabled=True, hard_stop_pct=0.03))
    _override_bars(hub, date(2026, 8, 4), {
        time(9, 35): {"open": 12.31, "high": 12.31, "low": 12.31, "close": 12.31,
                      "amount": 100.0 * 12.31},
        time(9, 40): {"open": 12.20, "high": 12.20, "low": 12.12, "close": 12.12,
                      "amount": 100.0 * 12.12},
    })
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600001.SH"]
    assert len(got) == 1
    t = got[0]
    assert t["reason"] == "TAIL_VWAP_EXIT", f"跌破均价应触发 VWAP 止损，实得 {t}"
    assert float(t["pnl"]) < 0


def test_exit_open_momentum_hold():
    """隔夜动量（2026-08-13 四段式革命）：高开 > +0.5% 卖半仓后，open>昨收 且
    首根 bar 量 ≥ 2.0×T日bar均量（300 ≥ 2×5500/48≈229）→ 剩余取消止盈，
    仅保留 VWAP 保底，持至 10:30 市价离场（TAIL_MOMENTUM_EXIT）。"""
    hub, bt = _exit_reasons()
    _override_bars(hub, date(2026, 8, 4), {
        time(9, 35): {"open": 12.42, "high": 12.46, "low": 12.36, "close": 12.44,
                      "volume": 300.0, "amount": 300.0 * 12.44}})
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600001.SH"]
    assert len(got) == 2, f"动量分支应为半仓+10:30 两笔，实得 {got}"
    assert got[0]["reason"] == "TAIL_HALF_OPEN"
    assert got[-1]["reason"] == "TAIL_MOMENTUM_EXIT", \
        "放量高开应持至 10:30 离场而非提前止盈"


def _crash_index(hub: FakeHub, day: date) -> FakeHub:
    """补 30 天高位指数后把当日砸到 3800 → 收盘 < MA20（弱市）。"""
    extra = [dict(date=pd.Timestamp(date(2026, 7, 27) - timedelta(days=i)),
                  symbol="000300.SH", open=4200.0, high=4200.0, low=4200.0,
                  close=4200.0, volume=1, amount=4200.0) for i in range(1, 31)]
    hub.index_daily = pd.concat([pd.DataFrame(extra), hub.index_daily],
                                 ignore_index=True).sort_values("date").reset_index(drop=True)
    mask = pd.to_datetime(hub.index_daily["date"]).dt.date == day
    hub.index_daily.loc[mask, "close"] = 3800.0
    return hub


def test_market_filter_blocks_and_toggle():
    """② 大市温度计弹性仓位（V4.0 熊市反击版）：

    条件A：指数 ≥ MA20 且广度达标 → 满仓档（ratio=1.0）追涨模式正常出票；
            adv ≤ weak_market_adv_min → 强市广度闸门空仓；
    条件B：指数破 MA 但 adv ≥ breadth_block_below(1000) → 侦察兵仓位档
            （ratio=weak_market_position_ratio=0.2）；V5 不切买跌带，统一涨幅带照常选股；
    条件C：adv < breadth_block_below → 绝对空仓；关闭过滤器后恢复。"""
    day = date(2026, 8, 5)

    # 基线：恒定指数 4000 == MA20（条件A）+ 放宽广度（上涨4 > 3）→ 满仓档正常出票
    hub = build_hub("full")
    scr = TailPickScreener(_base_cfg())
    assert scr.screen(hub, day, list(hub.instruments), _instr_map(hub),
                      minute_available=True), "基线应出票"
    assert scr.regime_ratio == 1.0, "条件A 广度达标应为满仓档"

    # 强市广度闸门：生产默认阈值 2500，合成市场上涨家数 4 ≤ 2500 → 空仓拦截
    # （显式关 V6：本用例验证 V5 追强势语义）
    scr_prod = TailPickScreener(TailPickConfig(industry_map_path="", defense_mode=False))
    assert not scr_prod.screen(hub, day, list(hub.instruments), _instr_map(hub),
                               minute_available=True), "强市广度闸门应拦截缩量日"
    assert scr_prod.regime_ratio == 0.0

    # 弱市 + 生产默认 breadth_block_below=1000：合成 adv=4 < 1000 → 绝对空仓拦截
    hubA = _crash_index(build_hub("full"), day)
    picks = scr_prod.screen(hubA, day, list(hubA.instruments), _instr_map(hubA),
                            minute_available=True)
    assert not picks, "弱市且上涨家数 < breadth_block_below 应绝对空仓"
    assert scr_prod.regime_ratio == 0.0

    # 侦察兵仓位档（V5）：弱市仓位 0.2，但 V5 不切买跌带——统一涨幅带下
    # 合成票 +3%/+2.5% 仍在带内应正常出票（V4 才会切买跌带全剔）
    scr_weak = TailPickScreener(_base_cfg())
    picks = scr_weak.screen(hubA, day, list(hubA.instruments), _instr_map(hubA),
                            minute_available=True)
    assert scr_weak.regime_ratio == 0.2, "弱市广度达标应为侦察兵仓位档"
    assert scr_weak.regime_weak, "指数跌破 MA 应置位弱市标记"
    assert {p.symbol for p in picks} == {"600001.SH", "300001.SZ"}, \
        "V5 弱市不切买跌带，统一带内候选应照常出票（仅仓位降至 0.2）"

    # 绝对空仓：弱市 + 只砸 B/C → 上涨 2 < breadth_block_below 3 → 拦截；关闭过滤器恢复出票
    hubB = _crash_index(build_hub("full"), day)
    maskB = (pd.to_datetime(hubB.daily["date"]).dt.date == day) & \
            hubB.daily["symbol"].isin(["600002.SH", "600003.SH"])
    hubB.daily.loc[maskB, "close"] = hubB.daily.loc[maskB, "prev_close"] * 0.99
    scr_blk = TailPickScreener(_base_cfg(breadth_block_below=3))
    assert not scr_blk.screen(hubB, day, list(hubB.instruments), _instr_map(hubB),
                              minute_available=True), "弱市跌多涨少应绝对空仓"
    assert scr_blk.regime_ratio == 0.0
    scr_off = TailPickScreener(_base_cfg(market_filter_enabled=False))
    assert scr_off.screen(hubB, day, list(hubB.instruments), _instr_map(hubB),
                          minute_available=True), "关闭大市过滤后应恢复出票"


def _mk_breadth_daily(adv: int, total: int, day: date) -> pd.DataFrame:
    """构造当日广度帧：前 adv 只上涨（10.0→10.1），其余下跌（10.0→9.9）。"""
    rows = []
    for i in range(total):
        rows.append(dict(date=pd.Timestamp(day), symbol=f"S{i:05d}",
                         prev_close=10.0, close=10.1 if i < adv else 9.9))
    return pd.DataFrame(rows)


def test_market_regime_tiers_direct():
    """温度计四档直测（V4.0 熊市反击版）：满仓/强市广度闸门/震荡市侦察兵/
    弱市侦察兵/绝对空仓。"""
    day = date(2026, 8, 5)
    scr = TailPickScreener(TailPickConfig(industry_map_path="", defense_mode=False))

    def hub_up() -> FakeHub:
        # 指数不足 MA 窗口天数 → best-effort 按站上 MA 处理（强市）
        return FakeHub(pd.DataFrame(), pd.DataFrame(), {}, pd.DataFrame(_index_rows(_DAYS)))

    hub_dn = _crash_index(FakeHub(pd.DataFrame(), pd.DataFrame(), {},
                                  pd.DataFrame(_index_rows(_DAYS))), day)

    ratio, why = scr._market_regime_ratio(hub_up(), day, _mk_breadth_daily(3000, 4000, day))
    assert ratio == 1.0 and scr.regime_ratio == 1.0, "强市广度达标应满仓档"

    ratio, why = scr._market_regime_ratio(hub_up(), day, _mk_breadth_daily(2500, 4000, day))
    assert ratio == 0.0 and "强市广度" in why, f"adv≤2500 应触发强市广度闸门，实得 {why}"

    ratio, why = scr._market_regime_ratio(hub_dn, day, _mk_breadth_daily(1500, 4000, day))
    assert ratio == 0.2 and "震荡市" in why, f"1000~2500 应为震荡市侦察兵档，实得 {why}"

    ratio, why = scr._market_regime_ratio(hub_dn, day, _mk_breadth_daily(3000, 4000, day))
    assert ratio == 0.2 and "弱市" in why, f">2500 应为弱市侦察兵档，实得 {why}"

    ratio, why = scr._market_regime_ratio(hub_dn, day, _mk_breadth_daily(800, 4000, day))
    assert ratio == 0.0 and "绝对空仓" in why, f"adv<1000 应绝对空仓，实得 {why}"


def test_strict_trend_gate_hard_empty():
    """选项A 大盘趋势硬空仓（2026-08-15）：只在沪深300 > MA(market_ma_days) 时
    允许交易——站上满仓档（仍过强市广度闸门），跌破绝对空仓，
    即使 adv 充足也不做弱市试错（优先级高于 V7/广度分档）。"""
    day = date(2026, 8, 5)
    cfg = TailPickConfig(strict_trend_gate=True, market_ma_days=60,
                         industry_map_path="")
    scr = TailPickScreener(cfg)

    def _idx_hub(close_day: float, level: float = 4000.0) -> FakeHub:
        # 75 天指数历史（含当日）：前 74 天恒定 level，当日 close_day
        rows = [dict(date=pd.Timestamp(day - timedelta(days=i)), symbol="000300.SH",
                     open=level, high=level, low=level,
                     close=close_day if i == 0 else level,
                     volume=1, amount=level) for i in range(75)]
        return FakeHub(pd.DataFrame(), pd.DataFrame(), {}, pd.DataFrame(rows))

    # 站上 MA60 + 广度达标 → 满仓档
    ratio, why = scr._market_regime_ratio(_idx_hub(4000.0), day,
                                          _mk_breadth_daily(3000, 4000, day))
    assert ratio == 1.0 and scr.regime_ratio == 1.0, f"站上MA60应满仓档，实得 {why}"

    # 站上 MA60 但广度不足 → 强市广度闸门空仓（V5 行为保留）
    ratio, why = scr._market_regime_ratio(_idx_hub(4000.0), day,
                                          _mk_breadth_daily(2500, 4000, day))
    assert ratio == 0.0 and "强市广度" in why, f"站上但广度不足应拦截，实得 {why}"

    # 跌破 MA60（前74天4200，当日砸3800 → MA60≈4137）：即使 adv 充足也绝对空仓
    ratio, why = scr._market_regime_ratio(_idx_hub(3800.0, level=4200.0), day,
                                          _mk_breadth_daily(3000, 4000, day))
    assert ratio == 0.0 and "趋势硬空仓" in why, \
        f"跌破MA60应硬空仓（不做弱市试错），实得 {why}"
    assert not scr.regime_weak, "硬空仓档不应置位弱市标记（无侦察兵试错）"


def test_config_from_settings():
    try:
        from qmt_trade.core.config import Settings
    except Exception:  # noqa: BLE001
        pytest.skip("Settings 不可用")
    s = Settings.load("config/settings.yaml")
    blk = s.section("strategies.tail_pick")
    assert blk is not None, "settings.yaml 应包含 strategies.tail_pick"
    # 四段式革命 + 混合方案回调（2026-08-13）阈值；V6.0 涨幅带改为 1%~3.5%
    assert blk.get("min_pct_change") == 0.01
    assert blk.get("max_pct_change") == 0.035
    assert blk.get("min_volume_ratio") == 1.0
    assert blk.get("volume_ladder_seg_tolerance") == 0.9
    assert blk.get("chip_vwap_tolerance_pct") == 0.01
    assert blk.get("breadth_block_below") == 1000
    # 时间不对称离场 + 板块效应新参数
    assert blk.get("gapup_threshold_pct") == 0.005
    assert blk.get("sector_enabled") is True
    # 既有纪律参数与用户手改值
    assert blk.get("overnight_stop_pct") == 0.03
    assert blk.get("max_positions") == 5
    assert blk.get("universe_top_n") == 100
    # V4.0 熊市反击版：买跌带/缩量过滤/硬止损/侦察兵仓位
    assert blk.get("weak_min_pct_change") == -0.01
    assert blk.get("weak_max_pct_change") == 0.015
    assert blk.get("shrink_vol_max_ratio") == 1.2
    assert blk.get("vol_spike_exclude_ratio") == 2.0
    assert blk.get("hard_stop_pct") == 0.012
    assert blk.get("take_profit_pct") == 0.028
    assert blk.get("vwap_exit_enabled") is False
    assert blk.get("weak_market_position_ratio") == 0.2
    # V7.0 强势回归一年回测 -48.7% 证伪 → 2026-08-15 回滚；选项A：选股回退 V5
    # （defense_mode/v7_strong_mode 双关）；2026-08-15 策略冻结：MA60 硬空仓同样证伪
    # （-22.45%），strict_trend_gate 回退关闭、market_ma_days 回 20（纯 V5 原始配置）。
    assert blk.get("defense_mode") is False
    assert blk.get("v7_strong_mode") is False
    assert blk.get("strict_trend_gate") is False
    assert blk.get("market_ma_days") == 20
    assert blk.get("low_ma_days") == 20
    assert blk.get("max_amplitude_pct") == 0.05
    assert blk.get("v6_low_cut_pct") == 0.003
    assert blk.get("v6_be_trigger_pct") == 0.008
    assert blk.get("v6_take_profit_pct") == 0.02
    assert blk.get("v6_tp_sell_ratio") == 0.7
    assert blk.get("v6_trail_pullback_pct") == 0.012


# ----------------------------------------------------------------- 板块效应
def test_sector_bottom_exclude_and_boost():
    """板块效应（2026-08-13 四段式革命）：当日行业涨幅排名后 sector_bottom_n
    的候选直接剔除；前 sector_top_n 行业的标的标记 sector_boost。"""
    day = date(2026, 8, 5)
    # 剔除变体：A/B/C 同属唯一行业 → 排名后 1 即该行业 → A/C 剔除，B 本就涨幅超界，
    # D 无行业映射中性放行
    hub = build_hub("full")
    scr = TailPickScreener(_base_cfg(sector_bottom_n=1))
    scr.industry_map = {"600001.SH": "弱势行业", "600002.SH": "弱势行业",
                        "600003.SH": "弱势行业"}
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub),
                       minute_available=True)
    assert {p.symbol for p in picks} == {"300001.SZ"}, \
        f"后位行业候选应被剔除、无映射者中性放行，实得 {[p.symbol for p in picks]}"
    # 加权变体：A/C/D 同属唯一行业 → 排名前 1 → 通过的 A/D 均标记 sector_boost
    hub2 = build_hub("full")
    scr2 = TailPickScreener(_base_cfg(sector_top_n=1, sector_bottom_n=0))
    scr2.industry_map = {"600001.SH": "热门行业", "600003.SH": "热门行业",
                         "300001.SZ": "热门行业"}
    picks2 = scr2.screen(hub2, day, list(hub2.instruments), _instr_map(hub2),
                         minute_available=True)
    assert {p.symbol for p in picks2} == {"600001.SH", "300001.SZ"}
    assert all(p.sector_boost for p in picks2), "前位行业候选应标记 sector_boost"


def test_sector_boost_position_doubled():
    """板块前 N 命中者买入名义额 ×sector_boost_mult(2.0)：同一标的在板块加权
    开/关两次运行中的买入股数应约为 2 倍（多标的依次买入会递减现金，故不用
    跨标的横向对比）。"""

    def _buy_qty(sector_top_n: int) -> dict[str, int]:
        cfg = _base_cfg(sector_top_n=sector_top_n, sector_bottom_n=0)
        hub = build_hub("full")
        bt = TailPickBacktester(__fake_settings(), hub, initial_cash=1_000_000,
                                config=cfg, require_minute=None)
        bt.screener.industry_map = {"600001.SH": "热门行业", "600002.SH": "热门行业",
                                    "600003.SH": "热门行业"}
        res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
        qty: dict[str, int] = {}
        for t in res.trades:
            if t.order_id.startswith("tp_buy"):
                qty.setdefault(t.symbol, t.quantity)
        return qty, res

    boosted, res_b = _buy_qty(1)     # A 命中前 1 行业 → 名义额加倍
    plain, _ = _buy_qty(0)           # 不加权对照
    assert "600001.SH" in boosted and "600001.SH" in plain, \
        f"两运行首日均应买入 A，实得 {boosted} / {plain}"
    assert boosted["600001.SH"] == pytest.approx(plain["600001.SH"] * 2, rel=0.02), \
        f"热门行业仓位应加倍，A 加权={boosted['600001.SH']}股 对照={plain['600001.SH']}股"
    # 未命中热门行业的 D：A 加倍占用现金后，D 的名义额按剩余现金缩水
    # （max_notional = cash × position_fraction），股数应小于不加权对照且仍有成交
    assert boosted.get("300001.SZ", 0) > 0, "D 在加权运行中仍应买入"
    assert boosted["300001.SZ"] <= plain.get("300001.SZ", 0), \
        f"A 加倍占用现金后 D 应缩量，加权={boosted['300001.SZ']} 对照={plain.get('300001.SZ')}"
    boosts = [x for x in res_b.details
              if x.get("action") == "TAIL_BUY" and x.get("symbol") == "600001.SH"]
    assert boosts and boosts[0].get("sector_boost") is True, "买入留痕应记录板块加权"


# ----------------------------------------------------------------- V5 强趋势+缩量版
def test_weak_market_buy_dip_band():
    """V5：弱市不再切换买跌带——统一涨幅带 1%~3.5% 下微涨 +0.5% 在带外被剔，
    带内候选照常出票（仅仓位降至 0.2）；关闭 strong_trend_mode 回退 V4 买跌带语义。"""
    day = date(2026, 8, 5)
    hub = _crash_index(build_hub("full"), day)
    # A/D 改微涨 +0.5%（改日线 close 后分钟线不再匹配 → 无分钟线模式）
    for sym in ("600001.SH", "300001.SZ"):
        mask = (pd.to_datetime(hub.daily["date"]).dt.date == day) & (hub.daily["symbol"] == sym)
        hub.daily.loc[mask, "close"] = hub.daily.loc[mask, "prev_close"] * 1.005
    scr = TailPickScreener(_base_cfg())
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub),
                       minute_available=False)
    syms = {p.symbol for p in picks}
    assert scr.regime_weak and scr.regime_ratio == 0.2
    assert not ({"600001.SH", "300001.SZ"} & syms), \
        "微涨 +0.5% 在 V5 统一带（1%~3.5%）外应被剔除（非买跌带放行）"
    assert "600003.SH" in syms, "C +3% 在统一带内应 best-effort 放行（无分钟线）"
    # V4 回退路径：同样数据关闭 strong_trend_mode → 买跌带 -1%~+1.5% 放行微涨票
    hub2 = _crash_index(build_hub("full"), day)
    for sym in ("600001.SH", "300001.SZ"):
        mask = (pd.to_datetime(hub2.daily["date"]).dt.date == day) & (hub2.daily["symbol"] == sym)
        hub2.daily.loc[mask, "close"] = hub2.daily.loc[mask, "prev_close"] * 1.005
    scr4 = TailPickScreener(_base_cfg(strong_trend_mode=False,
                                      shrink_vol_max_ratio=3.0, vol_spike_exclude_ratio=5.0))
    picks4 = scr4.screen(hub2, day, list(hub2.instruments), _instr_map(hub2),
                         minute_available=False)
    syms4 = {p.symbol for p in picks4}
    assert {"600001.SH", "300001.SZ"} <= syms4, "V4 弱市买跌带应放行微涨 +0.5%"
    assert "600002.SH" not in syms4, "涨幅 6% 超出买跌带上界应被剔除"


def test_weak_market_shrink_filter():
    """V5 缩量上限：当日量比 ≥ shrink_volume_ratio_max 剔除——1.05× 通过、1.5× 剔除
    （阈值 1.2）。V5 不分强弱市，强市直接验证；带内默认量比 2.46 的 C 一并被剔。"""
    day = date(2026, 8, 5)
    hub = build_hub("full")
    for sym, mult in (("600001.SH", 1.05), ("300001.SZ", 1.5)):
        ddt = pd.to_datetime(hub.daily["date"])
        mask = (ddt.dt.date == day) & (hub.daily["symbol"] == sym)
        prev = hub.daily[(ddt.dt.date < day) & (hub.daily["symbol"] == sym)]
        vol5 = float(prev.tail(5)["volume"].astype(float).mean())
        hub.daily.loc[mask, "volume"] = int(vol5 * mult)
    cfg = _base_cfg(shrink_volume_ratio_max=1.2)
    scr = TailPickScreener(cfg)
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub),
                       minute_available=False)
    assert {p.symbol for p in picks} == {"600001.SH"}, \
        "量比 1.05 应通过缩量上限、1.5 与默认 2.46 应被剔除"


def test_shrink_volume_cap_exclude():
    """V5：量比 ≥ shrink_volume_ratio_max(1.2) 强市也全剔——放宽后恢复出票。"""
    hub = build_hub("full")
    day = date(2026, 8, 5)
    scr = TailPickScreener(_base_cfg(shrink_volume_ratio_max=1.2))
    picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub),
                       minute_available=True)
    assert not picks and not scr.regime_weak, "合成量比≈2.46 ≥ 1.2 强市应全部剔除"
    scr2 = TailPickScreener(_base_cfg(shrink_volume_ratio_max=3.0))
    assert scr2.screen(hub, day, list(hub.instruments), _instr_map(hub),
                       minute_available=True), "放宽缩量上限后应恢复出票"


def test_exit_hard_stop():
    """V4.0 改动二：-1.2% 硬止损 TAIL_HARD_STOP 市价离场，亏损落地。

    entry≈12.3233（含买入滑点）→ 止损线≈12.1754；首根 bar 微高开避开缺口保护分支
    （high<12.4465 不激活保本），第二根 low=12.10 ≤ 止损线触发硬止损。"""
    hub, bt = _exit_reasons()
    _override_bars(hub, date(2026, 8, 4), {
        time(9, 35): {"open": 12.31, "high": 12.32, "low": 12.28, "close": 12.30,
                      "amount": 100.0 * 12.30},
        time(9, 40): {"open": 12.28, "high": 12.28, "low": 12.10, "close": 12.13,
                      "amount": 100.0 * 12.13},
    })
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600001.SH"]
    assert got and got[0]["reason"] == "TAIL_HARD_STOP", \
        f"盘中触及 -1.2% 应触发硬止损，实得 {got}"
    assert float(got[0]["pnl"]) < 0, "硬止损应为亏损离场"


# ----------------------------------------------------------------- V6.0 低位防御版
def _defense_cfg(**kw) -> TailPickConfig:
    """V6 用例基线：合成日线仅 12 天 → MA 用 low_ma_days=5；关大市过滤避免广度干扰。"""
    kw.setdefault("defense_mode", True)
    kw.setdefault("low_ma_days", 5)
    kw.setdefault("market_filter_enabled", False)
    kw.setdefault("weak_market_adv_min", 0)
    return _base_cfg(**kw)


def _v6_day_rows(sym, closes, lows_off=0.015, highs_off=0.004, vol=1_000_000, turnover=0.07,
                 last_low=None):
    """按指定收盘序列造日线（先跌后涨 → 收盘<MA5 且低点抬高）。
    last_low 可显式指定末日最低价（构造创新低变体）。"""
    rows = []
    for i, d in enumerate(_DAYS[:len(closes)]):
        close = round(closes[i], 4)
        pc = round(closes[i - 1], 4) if i > 0 else round(close / 1.015, 4)
        low = last_low if (last_low is not None and i == len(closes) - 1) \
            else round(close * (1 - lows_off), 4)
        rows.append(dict(date=pd.Timestamp(d), symbol=sym, open=pc,
                         high=round(close * (1 + highs_off), 4),
                         low=low,
                         close=close, volume=vol, amount=vol * close,
                         turnover_rate=turnover, prev_close=pc,
                         limit_up=round(close * 1.1, 4), limit_down=round(close * 0.9, 4)))
    return rows


def _v6_hub(daily_by_sym, with_minute=False) -> FakeHub:
    daily: list = []
    for rows in daily_by_sym.values():
        daily.extend(rows)
    minute: list = []
    if with_minute:
        for sym, rows in daily_by_sym.items():
            for r in rows:
                minute.extend(_minute_rows(sym, r["date"].date(), r["close"], r["high"], True))
    inst = {sym: InstrumentInfo(symbol=sym, name=sym, industry="测试", list_date=date(2020, 1, 1),
                                total_share=2_000_000_000, float_share=1_000_000_000,
                                is_st=False, is_suspended=False, market_cap=20_000_000_000)
            for sym in daily_by_sym}
    return FakeHub(pd.DataFrame(daily), pd.DataFrame(minute), inst,
                   pd.DataFrame(_index_rows(_DAYS)))


_V6_SEL_DAY = _DAYS[6]   # 选股日 = 7 根收盘序列的末日


def test_v6_select_rules():
    """V6 三铁律：收盘<MA5+止跌+低振幅入选；高位(⑩)/创新低(⑪)/大振幅(⑫)各自剔除。"""
    base_closes = [11.20, 10.90, 10.60, 10.40, 10.10, 9.99, 10.14]  # MA5≈10.25 > 10.14，低点抬高，振幅1.9%
    hub = _v6_hub({"600010.SH": _v6_day_rows("600010.SH", base_closes)})
    scr = TailPickScreener(_defense_cfg())
    picks = scr.screen(hub, _V6_SEL_DAY, ["600010.SH"], _instr_map(hub), minute_available=False)
    assert {p.symbol for p in picks} == {"600010.SH"}, \
        f"低位止跌低振幅应入选，实得 {[p.reasons for p in picks]}"
    assert any("⑩MA5下方" in r for r in picks[0].reasons)
    # ⑩ 高位剔除：收盘抬到 MA5 上方（涨幅带同步放宽隔离变量）
    hub_hi = _v6_hub({"600010.SH": _v6_day_rows("600010.SH", base_closes[:-1] + [10.60])})
    assert not TailPickScreener(_defense_cfg(max_pct_change=0.10)).screen(
        hub_hi, _V6_SEL_DAY, ["600010.SH"], _instr_map(hub_hi), minute_available=False), \
        "收盘≥MA5 的高位票应被⑩剔除"
    # ⑪ 创新低剔除：末日最低价显式跌破前日低点（前日 low = 9.99×0.985 ≈ 9.840）
    hub_low = _v6_hub({"600010.SH": _v6_day_rows("600010.SH", base_closes, last_low=9.80)})
    assert not TailPickScreener(_defense_cfg()).screen(
        hub_low, _V6_SEL_DAY, ["600010.SH"], _instr_map(hub_low), minute_available=False), \
        "当日创新低应被⑪剔除"
    # ⑫ 大振幅剔除：(high-low)/昨收 ≥ 5%
    hub_amp = _v6_hub({"600010.SH": _v6_day_rows("600010.SH", base_closes, highs_off=0.04)})
    assert not TailPickScreener(_defense_cfg()).screen(
        hub_amp, _V6_SEL_DAY, ["600010.SH"], _instr_map(hub_amp), minute_available=False), \
        "振幅≥5% 应被⑫剔除"


# ----------------------------------------------------------------- V7.0 强势回归版
def _v7_cfg(**kw) -> TailPickConfig:
    """V7 用例基线：defense_mode 保持开（复用 V6 离场链）+ v7_strong_mode 切换选股端；
    合成日线短 → MA 用 low_ma_days=5；关大市过滤（硬空仓用例单独直测 regime）。"""
    kw.setdefault("defense_mode", True)
    kw.setdefault("v7_strong_mode", True)
    kw.setdefault("low_ma_days", 5)
    kw.setdefault("market_filter_enabled", False)
    return _base_cfg(**kw)


def _v7_day_rows(sym, closes, vol=1_000_000, turnover=0.07):
    """按指定收盘序列造日线（先回调后温和上涨 → 末日站上 MA5）。"""
    rows = []
    for i, d in enumerate(_DAYS[:len(closes)]):
        close = round(closes[i], 4)
        pc = round(closes[i - 1], 4) if i > 0 else round(close / 1.015, 4)
        rows.append(dict(date=pd.Timestamp(d), symbol=sym, open=pc,
                         high=round(close * 1.004, 4), low=round(close * 0.985, 4),
                         close=close, volume=vol, amount=vol * close,
                         turnover_rate=turnover, prev_close=pc,
                         limit_up=round(close * 1.1, 4), limit_down=round(close * 0.9, 4)))
    return rows


def test_v7_select_rules():
    """V7 强势回归：站上MA5+5日跑赢3%+缩量+涨幅带入选；跌破MA5(⑬)/跑赢不足(⑨)各自剔除。"""
    # 末日 10.44：MA5≈10.15 站上；⑨基准=iloc[-6]=10.12 → 超额 10.44/10.12-1≈3.16% ≥ 3%
    # （指数恒定 4000 → 指数5日涨幅 0，超额=个股涨幅）；末日涨幅 2.96% 在带内
    closes = [10.30, 10.12, 10.05, 10.02, 10.10, 10.14, 10.44]
    hub = _v6_hub({"600010.SH": _v7_day_rows("600010.SH", closes)})
    picks = TailPickScreener(_v7_cfg()).screen(
        hub, _V6_SEL_DAY, ["600010.SH"], _instr_map(hub), minute_available=False)
    assert {p.symbol for p in picks} == {"600010.SH"}, \
        f"站上MA5+跑赢3%+缩量温和上涨应入选，实得 {[p.reasons for p in picks]}"
    assert any("⑬站上MA5" in r for r in picks[0].reasons)
    assert any(r.startswith("⑨5日超额") for r in picks[0].reasons)
    # ⑬ 剔除：回调后反弹未站回 MA5（涨幅带内但收盘 < MA5，与 V6 方向完全相反）
    closes_below = [10.90, 10.80, 10.60, 10.50, 10.40, 10.12, 10.30]
    hub_below = _v6_hub({"600010.SH": _v7_day_rows("600010.SH", closes_below)})
    assert not TailPickScreener(_v7_cfg()).screen(
        hub_below, _V6_SEL_DAY, ["600010.SH"], _instr_map(hub_below), minute_available=False), \
        "收盘<MA5 应被⑬剔除（与 V6 方向完全相反）"
    # ⑨ 剔除：5日跑赢仅≈2.76% < 3%（仍站上 MA5，隔离变量只剩 ⑨）
    hub_weak = _v6_hub({"600010.SH": _v7_day_rows("600010.SH", closes[:-1] + [10.42])})
    assert not TailPickScreener(_v7_cfg()).screen(
        hub_weak, _V6_SEL_DAY, ["600010.SH"], _instr_map(hub_weak), minute_available=False), \
        "5日跑赢不足3% 应被⑨剔除"


def test_v7_regime_hard_empty():
    """V7 大盘前置硬空仓：指数站上 MA20 满仓档（不受广度闸门约束）；
    跌破 MA20 → 0 硬空仓，无弱市试错仓位。"""
    day = date(2026, 8, 5)
    scr = TailPickScreener(TailPickConfig(industry_map_path="", defense_mode=True,
                                          v7_strong_mode=True))

    def hub_up() -> FakeHub:
        # 指数不足 MA20 窗口天数 → best-effort 按站上 MA 处理
        return FakeHub(pd.DataFrame(), pd.DataFrame(), {}, pd.DataFrame(_index_rows(_DAYS)))

    # 广度不足（adv=2500 会触发 V5/V6 强市广度闸门）→ V7 仍满仓出击
    ratio, _ = scr._market_regime_ratio(hub_up(), day, _mk_breadth_daily(2500, 4000, day))
    assert ratio == 1.0 and scr.regime_ratio == 1.0, "V7 指数站上MA20 应满仓档（不看广度）"

    hub_dn = _crash_index(FakeHub(pd.DataFrame(), pd.DataFrame(), {},
                                  pd.DataFrame(_index_rows(_DAYS))), day)
    ratio, why = scr._market_regime_ratio(hub_dn, day, _mk_breadth_daily(3000, 4000, day))
    assert ratio == 0.0 and scr.regime_ratio == 0.0 and "V7硬空仓" in why, \
        f"指数跌破MA20 应硬空仓（即使广度3000达标），实得 {why}"


def _v6_exit_setup():
    """V6 离场回测：08-03 满足 V6 选股（回调后首日反弹）买入，08-04 分钟线可覆写。"""
    closes = [11.60, 11.30, 11.00, 10.70, 10.40, 10.20, 10.05, 10.20, 10.30]
    hub = _v6_hub({"600010.SH": _v6_day_rows("600010.SH", closes)}, with_minute=True)
    bt = TailPickBacktester(__fake_settings(), hub, initial_cash=1_000_000,
                            config=_defense_cfg(), require_minute=None)
    return hub, bt


def _v6_entry_price() -> float:
    hub, bt = _v6_exit_setup()
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    buys = [t for t in res.trades
            if t.symbol == "600010.SH" and t.order_id.startswith("tp_buy")]
    assert buys, "08-03 应按 V6 选股买入"
    return float(buys[0].price)


def test_v6_exit_gapcut():
    """低开 < 成本×0.997 → 竞价全砍 TAIL_V6_GAPCUT，亏损落地。"""
    entry = _v6_entry_price()
    hub, bt = _v6_exit_setup()
    _override_bars(hub, date(2026, 8, 4), {time(9, 35): {
        "open": round(entry * 0.99, 4), "high": round(entry * 0.99, 4),
        "low": round(entry * 0.988, 4), "close": round(entry * 0.99, 4)}})
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600010.SH"]
    assert [t["reason"] for t in got] == ["TAIL_V6_GAPCUT"], f"低开应全砍，实得 {got}"
    assert float(got[0]["pnl"]) < 0


def test_v6_exit_breakeven():
    """首根 bar 冲高≥成本×1.008 激活保本（不卖）→ 次根回落触及成本 → TAIL_V6_BE。"""
    entry = _v6_entry_price()
    hub, bt = _v6_exit_setup()
    _override_bars(hub, date(2026, 8, 4), {
        time(9, 35): {"open": round(entry * 1.002, 4), "high": round(entry * 1.012, 4),
                      "low": round(entry * 1.001, 4), "close": round(entry * 1.005, 4)},
        time(9, 40): {"open": round(entry * 1.005, 4), "high": round(entry * 1.005, 4),
                      "low": round(entry * 0.999, 4), "close": round(entry, 4)},
    })
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600010.SH"]
    assert [t["reason"] for t in got] == ["TAIL_V6_BE"], f"保本激活后回落应保本走，实得 {got}"


def test_v6_exit_tp70_and_trail():
    """触及成本×1.02 → 卖70% TAIL_V6_TP70；余仓从最高点回撤1.2% → TAIL_V6_TRAIL。"""
    entry = _v6_entry_price()
    hub, bt = _v6_exit_setup()
    _override_bars(hub, date(2026, 8, 4), {
        time(9, 35): {"open": round(entry * 1.002, 4), "high": round(entry * 1.005, 4),
                      "low": round(entry * 1.001, 4), "close": round(entry * 1.004, 4)},
        time(9, 40): {"open": round(entry * 1.005, 4), "high": round(entry * 1.026, 4),
                      "low": round(entry * 1.004, 4), "close": round(entry * 1.024, 4)},
        time(9, 45): {"open": round(entry * 1.024, 4), "high": round(entry * 1.024, 4),
                      "low": round(entry * 1.026 * 0.987, 4), "close": round(entry * 1.01, 4)},
    })
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600010.SH"]
    assert [t["reason"] for t in got] == ["TAIL_V6_TP70", "TAIL_V6_TRAIL"], f"实得 {got}"
    total = sum(t["shares"] for t in got)
    assert got[0]["shares"] == int(total * 0.7 // 100 * 100), "首笔应为70%仓位"


def test_v6_exit_time_exit():
    """平开窄幅震荡全窗口无触发 → 10:00 末根收盘市价离场 TAIL_V6_EXIT。"""
    entry = _v6_entry_price()
    hub, bt = _v6_exit_setup()
    _override_bars(hub, date(2026, 8, 4), {
        tm: {"open": round(entry, 4), "high": round(entry * 1.004, 4),
             "low": round(entry * 0.999, 4), "close": round(entry * 1.001, 4)}
        for tm in (time(9, 35), time(9, 40), time(9, 45), time(9, 50),
                   time(9, 55), time(10, 0))
    })
    res = bt.run(date(2026, 8, 3), date(2026, 8, 4))
    got = [t for t in res.closed_trades if t["symbol"] == "600010.SH"]
    assert [t["reason"] for t in got] == ["TAIL_V6_EXIT"], f"无触发应 10:00 离场，实得 {got}"


# ----------------------------------------------------------------- CLI 自检入口
def run_selfcheck() -> dict:
    """供 ``python -m qmt_trade tailpick validate`` 调用。

    用合成行情跑一遍「选股 + 回测」，确认 8 层筛选与一夜持股机制链路自洽。
    返回 {ok, lines}。
    """
    lines: list[str] = []
    ok = True
    try:
        hub = build_hub("full")
        cfg = _base_cfg()
        scr = TailPickScreener(cfg)
        day = date(2026, 8, 5)
        picks = scr.screen(hub, day, list(hub.instruments), _instr_map(hub), minute_available=True)
        lines.append(f"选股({day})：通过 {len(picks)} 只 -> {[p.symbol for p in picks]}")
        if not picks:
            ok = False
            lines.append("  ✗ 无任何标的通过 8 层筛选")
        if any(p.symbol in ("600002.SH", "600003.SH") for p in picks):
            ok = False
            lines.append("  ✗ 不合格标的（涨幅超界/尾盘筹码失败）漏网")

        bt = TailPickBacktester(__fake_settings(), hub, initial_cash=1_000_000, config=cfg,
                                require_minute=None)
        res = bt.run(date(2026, 8, 3), date(2026, 8, 7))
        lines.append(f"回测：权益曲线 {len(res.equity_curve)} 点，平仓 {len(res.closed_trades)} 笔，"
                     f"成交 {len(res.trades)} 笔")
        if res.metrics is None or len(res.closed_trades) < 1:
            ok = False
            lines.append("  ✗ 回测未产出平仓/指标")
        if res.closed_trades and not all(int(t["holding_days"]) == 1 for t in res.closed_trades):
            ok = False
            lines.append("  ✗ 持仓天数非 1（一夜持股纪律被破坏）")

        hub2 = build_hub("stop")
        # 合成市场仅 1 只股票，需放宽广度闸门（含强市 adv>2500 默认档）才能出票
        cfg2 = _base_cfg(weak_market_adv_min=0)
        bt2 = TailPickBacktester(__fake_settings(), hub2, initial_cash=1_000_000, config=cfg2,
                                 require_minute=None)
        res2 = bt2.run(date(2026, 8, 3), date(2026, 8, 4))
        stops = [t for t in res2.closed_trades
                 if t.get("reason") in ("TAIL_STOP", "TAIL_GAPSTOP", "TAIL_LOWOPEN_CUT")]
        lines.append(f"隔夜止损场景：触发 {len(stops)} 笔开盘保护/9:45 砍仓")
        if not stops:
            ok = False
            lines.append("  ✗ 隔夜跳空未触发开盘保护/9:45 砍仓")
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(f"  ✗ 异常：{type(exc).__name__}: {exc}")
    return {"ok": ok, "lines": lines}


def __fake_settings():
    """返回仅含回测/执行配置的极简 Settings 替身（避免加载完整 settings 依赖链）。"""

    class _S:
        def section(self, key):
            if key == "execution.costs":
                return {"commission_rate": 0.00025, "commission_min": 5.0,
                        "stamp_duty_rate": 0.0005, "transfer_fee_rate": 1.0e-05,
                        "base_slippage": 0.002}
            if key == "backtest":
                return {"initial_cash": 1_000_000, "benchmark": "000300.SH",
                        "trading_days_per_year": 244, "risk_free_rate": 0.02}
            if key == "portfolio":
                return {"base_risk_pct": 0.006, "max_weight_pct": 0.15,
                        "max_order_value_ratio": 0.10, "max_volume_ratio_of_adv": 0.05,
                        "cash_usage_ratio": 0.95, "total_risk_budget": 0.03, "min_shares": 100}
            return {}

        def get(self, key, default=None):
            return default

    return _S()


if __name__ == "__main__":  # pragma: no cover
    rep = run_selfcheck()
    print("尾盘选股法机制自检：", "通过 ✅" if rep["ok"] else "失败 ❌")
    for ln in rep["lines"]:
        print("  " + ln)
