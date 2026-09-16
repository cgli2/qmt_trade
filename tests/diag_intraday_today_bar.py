"""盘中「当日日线 bar 是否存在」的决定性证据（性能修复 2026-09-16 回归排查）。

背景：第一版日历收敛把探测上界压到「最近一个已收盘交易日」，即除 POST_CLOSE 外
一律排除当日。受控合成实验发现这会挡住盘中正在形成的当日 bar（10:30/12:00/14:30
全被挡），行情页将整日冻结在昨收。收窄为「仅 CLOSED/PRE_OPEN 收敛」之后，必须用
**真实数据源**确认两件事：

  1. 盘中当日日线 bar 是否真的存在？（存在 → 收窄正确；不存在 → 需重新设计）
  2. 盘中发起当日缺口探测耗时多少？（必须远小于盘前失败的 ~6s，否则用户仍卡）

本脚本在进程内直连 provider，不打开生产 qmt.duckdb（Settings 跳过 read_active、
store 用内存库），因此不会与常驻后端争 DuckDB 单写者锁。

运行：python tests/diag_intraday_today_bar.py
（遵守项目规则：所有测试用 Python 脚本，禁用 curl。）
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，编不出 ✔/⚠ 会 UnicodeEncodeError 打断结论输出
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from qmt_trade.app import _make_provider          # noqa: E402
from qmt_trade.core.clock import Session, calendar  # noqa: E402
from qmt_trade.core.config import (                # noqa: E402
    DEFAULT_ENV, DEFAULT_SETTINGS, Settings, load_dotenv,
    _load_and_migrate_strategy_configs, _load_yaml_mapping,
)
from qmt_trade.datahub.manager import DataHub      # noqa: E402
from qmt_trade.datahub.providers.base import Capability  # noqa: E402
from qmt_trade.datahub.types import Adjust, Freq   # noqa: E402
from qmt_trade.storage.db import Database          # noqa: E402
from qmt_trade.storage.market import MarketRepository  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

SYMBOL = "001256.SZ"        # 上一轮实测的策略推荐样本之一
LOOKBACK_DAYS = 180


def _load_settings_nodb() -> Settings:
    """复刻 Settings.load() 装配步骤但跳过 read_active（避免与常驻后端争 DuckDB 锁）。"""
    load_dotenv(DEFAULT_ENV)
    raw = _load_yaml_mapping(DEFAULT_SETTINGS)
    raw = _load_and_migrate_strategy_configs(DEFAULT_SETTINGS, raw)
    inst = Settings(raw, env_overlay=True)
    inst._path = DEFAULT_SETTINGS
    return inst


def _providers(settings) -> list:
    wanted: list[str] = []
    for names in (settings.section("datahub.priority") or {}).values():
        for n in names or ():
            if n not in wanted:
                wanted.append(n)
    out = []
    for name in wanted:
        try:
            p = _make_provider(name, settings)
        except Exception as exc:                      # noqa: BLE001
            print(f"  [skip] {name}: {type(exc).__name__}: {exc}")
            continue
        if p is not None and p.supports(Capability.BARS):
            out.append(p)
    return out


def main() -> int:
    settings = _load_settings_nodb()
    now = datetime.now()
    today = now.date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    prev_td = calendar.prev_trading_day(today)

    print("=" * 78)
    print(f"now = {now:%Y-%m-%d %H:%M:%S}   session = {calendar.session_of(now).name}")
    print(f"today = {today}   prev_trading_day = {prev_td}   请求区间 = [{start}, {today}]")
    print(f"_NO_TODAY_BAR_SESSIONS = {sorted(s.name for s in DataHub._NO_TODAY_BAR_SESSIONS)}")
    print("=" * 78)

    # ---- 1. 逐源直问：当日 bar 存在吗？耗时多少？ ----
    print(f"\n[1] 逐源直问 get_bars({SYMBOL}, D1, {prev_td}, {today})  —— 只取 2 天，最小化耗时")
    provs = _providers(settings)
    if not provs:
        print("无可用真实数据源，无法取证")
        return 1
    today_present: dict[str, bool] = {}
    for p in provs:
        t = time.perf_counter()
        try:
            df = p.get_bars([SYMBOL], Freq.D1, prev_td, today, Adjust.HFQ)
        except Exception as exc:                      # noqa: BLE001
            print(f"  {p.name:<10} EXC  {type(exc).__name__}: {exc}  ({time.perf_counter()-t:.2f}s)")
            today_present[p.name] = False
            continue
        el = time.perf_counter() - t
        if df is None or df.empty:
            print(f"  {p.name:<10} EMPTY                       ({el:.2f}s)")
            today_present[p.name] = False
            continue
        dates = sorted(pd.to_datetime(df["date"]).dt.date.unique())
        has_today = today in dates
        today_present[p.name] = has_today
        row = df[pd.to_datetime(df["date"]).dt.date == today]
        close = float(row["close"].iloc[0]) if not row.empty else float("nan")
        vol = float(row["volume"].iloc[0]) if not row.empty else float("nan")
        print(f"  {p.name:<10} rows={len(df):<3} dates={[str(d) for d in dates]}"
              f"  当日bar={'YES' if has_today else 'NO '}"
              f"  close={close:.2f} vol={vol:.0f}  ({el:.2f}s)")

    # ---- 2. 走完整 DataHub（含降级链 + 增量分支），测端到端耗时与末端日期 ----
    print(f"\n[2] 经 DataHub 全链路：cold + 4 次重复（模拟用户连点/切股）")
    hub = DataHub(settings, provs,
                  store=MarketRepository(Database(":memory:", schema="market")))
    t = time.perf_counter()
    df = hub.get_bars([SYMBOL], Freq.D1, start, today, adjust=Adjust.HFQ, validate=False)
    cold = time.perf_counter() - t
    end_date = pd.to_datetime(df["date"]).max().date() if not df.empty else None
    print(f"  cold   = {cold:6.2f}s  rows={len(df):<4} 末端={end_date}  "
          f"含当日={'YES' if end_date == today else 'NO'}")
    for i in range(4):
        t = time.perf_counter()
        d2 = hub.get_bars([SYMBOL], Freq.D1, start, today, adjust=Adjust.HFQ, validate=False)
        e2 = pd.to_datetime(d2["date"]).max().date() if not d2.empty else None
        print(f"  repeat{i+1} = {time.perf_counter()-t:6.2f}s  rows={len(d2):<4} 末端={e2}")

    # ---- 3. 收窄后的 _clamp_probe_end 在各时段的取值（纯函数，注入时钟） ----
    print("\n[3] _clamp_probe_end(today) 在各时段的取值（收窄后应只在盘前/非交易日收敛）")
    samples = [
        (datetime(today.year, today.month, today.day, 8, 49), "盘前 08:49"),
        (datetime(today.year, today.month, today.day, 9, 14), "盘前 09:14"),
        (datetime(today.year, today.month, today.day, 9, 15), "开盘竞价 09:15"),
        (datetime(today.year, today.month, today.day, 9, 25), "静默 09:25"),
        (datetime(today.year, today.month, today.day, 10, 30), "上午盘 10:30"),
        (datetime(today.year, today.month, today.day, 12, 0), "午休 12:00"),
        (datetime(today.year, today.month, today.day, 14, 30), "下午盘 14:30"),
        (datetime(today.year, today.month, today.day, 14, 58), "收盘竞价 14:58"),
        (datetime(today.year, today.month, today.day, 15, 30), "收盘后 15:30"),
        (datetime(today.year, today.month, today.day, 20, 0), "夜间 20:00"),
    ]
    for s, label in samples:
        if not calendar.is_trading_day(s.date()):
            print(f"  {label:<16} 今天非交易日，跳过")
            continue
        sess = calendar.session_of(s)
        got = hub._clamp_probe_end(today, now=s)
        blocked = got < today
        print(f"  {label:<16} session={sess.name:<14} clamp={got}  "
              f"{'排除当日' if blocked else '含当日'}")
    sat = calendar.prev_trading_day(today) + timedelta(days=(5 - calendar.prev_trading_day(today).weekday()) % 7 or 7)
    if calendar.is_trading_day(sat):
        print(f"  下个非交易日 {sat}   竟是交易日，跳过")
    else:
        m = datetime(sat.year, sat.month, sat.day, 10, 0)
        got = hub._clamp_probe_end(sat, now=m)
        print(f"  {'非交易日 ' + str(sat):<16} session={calendar.session_of(m).name:<14}"
              f" clamp={got}  排除当日={'YES' if got < sat else 'NO'}")

    # ---- 4. 结论 ----
    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    any_today = any(today_present.values())
    print(f"当日 bar 存在情况：{today_present}")
    if calendar.session_of(now) in (Session.MORNING, Session.LUNCH, Session.AFTERNOON,
                                    Session.AUCTION_CLOSE, Session.POST_CLOSE):
        if any_today:
            print("✔ 盘中当日 bar 确实存在 → 收窄到「仅 CLOSED/PRE_OPEN 收敛」是必要的，"
                  "第一版收敛会造成盘中数据冻结的回归。")
        else:
            print("⚠ 盘中竟无当日 bar → 需重新审视：可能数据源当日 bar 延迟，"
                  "此时第一版收敛并无回归，收窄只是放宽了不必要的限制。")
    else:
        print(f"⚠ 当前 session={calendar.session_of(now).name} 非盘中，"
              "「当日 bar 是否存在」的结论不适用于盘中，请在 09:30 后重跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
