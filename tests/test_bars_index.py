"""回测行情内存索引提速 —— 正确性单测（合成数据，无需 QMT）。

验证：engine.py 新增的「预热全量日线 → 按 (标的,复权) 建内存索引 → 循环切片」
与旧的「逐标的 get_bars(单标的, fixed_start, day) 再按日过滤」在撮合价/市值标记
上**逐字段一致**。若不一致说明提速改坏了语义，必须拦下。
"""
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, ".")


# ---- 复刻 engine.py 的索引构建与切片逻辑（与源码保持同语义）----
def build_bars_index(panel: pd.DataFrame):
    """engine.run() 预热段：把全量日线按 (标的, 复权) 建内存索引。"""
    df = panel.copy()
    df["date"] = pd.to_datetime(df["date"])
    return {s: sub.sort_values("date").reset_index(drop=True)
            for s, sub in df.groupby("symbol")}


def lookup_bar(index: dict, symbol: str, day: date):
    """engine._lookup_bar：精确等于 day 的 bar（停牌/缺失日 → None，与旧路径一致）。"""
    sub = index.get(symbol)
    if sub is None or sub.empty:
        return None
    sd = sub[sub["date"] == pd.Timestamp(day)]
    if sd.empty:
        return None
    return sd.iloc[-1]


def last_prices(index: dict, syms, day):
    out = {}
    for sym in syms:
        sub = index.get(sym)
        if sub is None or sub.empty:
            continue
        sd = sub[sub["date"] <= pd.Timestamp(day)]
        if not sd.empty:
            out[sym] = float(sd.iloc[-1]["close"])
    return out


# ---- 旧路径参考实现：模拟 DataHub.get_bars(单标的) 的返回（已按 end 切片）----
class MockHub:
    def __init__(self, panel: pd.DataFrame):
        self.panel = panel

    def get_bars(self, symbols, freq, start, end, adjust):
        syms = [symbols] if isinstance(symbols, str) else list(symbols)
        df = self.panel[self.panel["symbol"].isin(set(syms))].copy()
        df["date"] = pd.to_datetime(df["date"])
        # DataHub 范围缓存：返回 [start, ∞)，再按 end 切片
        df = df[df["date"] <= pd.Timestamp(end)]
        if len(syms) == 1:
            df = df[df["symbol"] == syms[0]]
        return df.sort_values(["symbol", "date"]).reset_index(drop=True)


def bar_from_old(hub: MockHub, sym: str, day: date):
    df = hub.get_bars(sym, "D1", date(2024, 1, 1), day, "NONE")
    if df is None or df.empty:
        return None
    day_df = df[df["date"].dt.date == day] if "date" in df.columns else df
    if day_df.empty:
        return None
    return day_df.iloc[-1]


def main():
    rng = np.random.default_rng(42)
    syms = [f"{i:06d}.SH" for i in range(1, 31)]  # 30 只标的
    start = date(2024, 1, 1)
    days = [start + timedelta(days=k) for k in range(400) if (start + timedelta(days=k)).weekday() < 5]
    rows = []
    for s in syms:
        px = 10.0 + rng.random() * 20
        for d in days:
            px *= (1 + rng.normal(0, 0.02))
            rows.append({
                "symbol": s, "date": d,
                "open": px * 0.99, "high": px * 1.02, "low": px * 0.98,
                "close": px, "volume": rng.integers(1e5, 1e6),
                "amount": px * rng.integers(1e5, 1e6),
                "limit_up": px * 1.1, "limit_down": px * 0.9,
            })
    panel = pd.DataFrame(rows)

    index = build_bars_index(panel)
    hub = MockHub(panel)

    # 1) _lookup_bar 与旧路径逐字段一致（含「周末/停牌缺失日」返回上一根的情形）
    mism = 0
    for sym in syms:
        for d in days[::7]:  # 抽样
            new_row = lookup_bar(index, sym, d)
            old_row = bar_from_old(hub, sym, d)
            assert (new_row is None) == (old_row is None), f"{sym} {d} None 不一致"
            if new_row is None:
                continue
            for col in ("open", "high", "low", "close", "limit_up", "limit_down"):
                if abs(float(new_row[col]) - float(old_row[col])) > 1e-9:
                    mism += 1
                    print(f"  ✗ {sym} {d} 列 {col}: {new_row[col]} vs {old_row[col]}")
    print(f"[1] _lookup_bar 与旧 get_bars 逐字段一致：{'OK' if mism == 0 else f'{mism} 处偏差'}")

    # 2) 缺失日（非交易日/停牌）应返回 None —— 与旧路径一致（不回落上一根，避免停牌票用陈旧价下单）
    gap_day = date(2024, 1, 6)  # 周六，panel 仅含工作日，必缺失
    assert gap_day not in days
    nb = lookup_bar(index, syms[0], gap_day)
    ob = bar_from_old(hub, syms[0], gap_day)
    ok_gap = (nb is None) and (ob is None)
    print(f"[2] 缺失日返回 None（不回落陈旧价）：{'OK' if ok_gap else 'FAIL'}")

    # 3) _last_prices 与旧路径一致
    held = syms[:8]
    new_lp = last_prices(index, held, days[-1])
    old_lp = {s: float(bar_from_old(hub, s, days[-1])["close"]) for s in held}
    ok_lp = new_lp == old_lp and len(new_lp) == len(held)
    print(f"[3] _last_prices 与旧路径一致：{'OK' if ok_lp else 'FAIL'}")

    # 4) 索引键区分复权（NONE/HFQ）—— 确认建两套互不影响
    idx_none = build_bars_index(panel)
    hfq = panel.copy()
    hfq["close"] = hfq["close"] * 1.05  # 模拟后复权
    idx_hfq = build_bars_index(hfq)
    sep = idx_none[syms[0]]["close"].iloc[-1] != idx_hfq[syms[0]]["close"].iloc[-1]
    print(f"[4] NONE/HFQ 索引分列（建两套独立 dict）：{'OK' if sep else 'FAIL（共用同一帧会串价）'}")

    all_ok = mism == 0 and ok_gap and ok_lp and sep
    print("ALL PASS" if all_ok else "FAILED")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
