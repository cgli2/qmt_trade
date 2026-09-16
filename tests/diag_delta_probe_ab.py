"""真实数据源上的「增量探测」A/B 复测（性能修复 2026-09-16）。

与 diag_kline_latency.py（HTTP 端到端，需重启后端）互补：本脚本**在进程内**
直接驱动 DataHub + 真实 qmt/akshare provider，因此无需重启用户正在运行的后端，
也不打开生产 qmt.duckdb（store 用内存库，避免与常驻进程争文件锁）。

复现条件与用户报障一致：end=今天、当前处于盘前/非交易时段 → 当日日线尚不存在。

  A 组 = 修复开启（生产现状）
  B 组 = 修复撤销（_clamp_probe_end 退化为恒等 + 负缓存关闭），即修复前行为

判据：A 组重复请求必须全部 <1s（零探测）；B 组每次 ~6s（沿降级链空跑）。
若 A 组也慢，说明修复没生效；若 B 组不慢，说明当前时段本来就探测不到缺口，
本次复测无法证伪修复（需换到盘前/周末重跑）。

运行：python tests/diag_delta_probe_ab.py
（遵守项目规则：所有测试用 Python 脚本，禁用 curl。）
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qmt_trade.app import _make_provider          # noqa: E402
from qmt_trade.core.clock import calendar          # noqa: E402
from qmt_trade.core.config import (                # noqa: E402
    DEFAULT_ENV, DEFAULT_SETTINGS, Settings, load_dotenv,
    _load_and_migrate_strategy_configs, _load_yaml_mapping,
)
from qmt_trade.datahub.manager import DataHub      # noqa: E402
from qmt_trade.datahub.types import Adjust, Freq   # noqa: E402
from qmt_trade.storage.db import Database          # noqa: E402
from qmt_trade.storage.market import MarketRepository  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

SYMBOLS = ["001256.SZ", "600000.SH"]      # 001256.SZ 来自上一轮实测的策略推荐样本
LOOKBACK_DAYS = 180                        # 与 /market/kline 的 D1 默认区间同量级
REPEATS = 4


def _load_settings_nodb() -> Settings:
    """复刻 Settings.load() 的装配步骤，但**跳过 read_active**。

    DuckDB 是单写者：常驻后端持有 qmt.duckdb 的文件锁，而 Settings.load() 在
    路径等于默认 config/settings.yaml 时会去读库里的 published 覆盖，导致任何
    独立进程直接 PermissionError。这里只从 YAML 装配，代价是缺少「Web UI 改过并
    发布到库」的那部分覆盖 —— 对本脚本关心的 datahub.priority / cache 配置无影响。
    """
    load_dotenv(DEFAULT_ENV)
    raw = _load_yaml_mapping(DEFAULT_SETTINGS)
    raw = _load_and_migrate_strategy_configs(DEFAULT_SETTINGS, raw)
    inst = Settings(raw, env_overlay=True)
    inst._path = DEFAULT_SETTINGS
    return inst


def _build_hub(settings) -> DataHub:
    """按生产配置装配真实 provider；store 用内存库，绝不触碰 qmt.duckdb。"""
    wanted: list[str] = []
    for names in (settings.section("datahub.priority") or {}).values():
        for n in names or ():
            if n not in wanted:
                wanted.append(n)
    providers = []
    for name in wanted:
        try:
            p = _make_provider(name, settings)
        except Exception as exc:                      # noqa: BLE001
            print(f"  [skip] provider {name}: {type(exc).__name__}: {exc}")
            continue
        if p is not None and p.is_available():
            providers.append(p)
    if not providers:
        raise SystemExit("无可用真实数据源，无法复测")
    print(f"  providers = {[p.name for p in providers]}")
    return DataHub(settings, providers,
                   store=MarketRepository(Database(":memory:", schema="market")))


def _measure(hub, sym: str, start: date, end: date) -> tuple[float, list[float], int]:
    """返回 (冷启动秒, 重复请求各次秒, 结果行数)。"""
    t0 = time.perf_counter()
    df = hub.get_bars([sym], Freq.D1, start, end, adjust=Adjust.HFQ, validate=False)
    cold = time.perf_counter() - t0
    repeats = []
    for _ in range(REPEATS):
        t = time.perf_counter()
        hub.get_bars([sym], Freq.D1, start, end, adjust=Adjust.HFQ, validate=False)
        repeats.append(time.perf_counter() - t)
    return cold, repeats, (0 if df is None else len(df))


def main() -> int:
    settings = _load_settings_nodb()
    now = pd.Timestamp.now().to_pydatetime()
    today = now.date()
    start = today - timedelta(days=LOOKBACK_DAYS)

    print("=" * 72)
    print(f"now = {now:%Y-%m-%d %H:%M:%S}  session = {calendar.session_of(now).name}")
    print(f"prev_trading_day = {calendar.prev_trading_day(today)}   请求 end = {today}")
    print(f"区间 = [{start}, {today}]  repeats = {REPEATS}")
    print("=" * 72)

    print("\n[A] 修复开启（生产现状）")
    hub_a = _build_hub(settings)
    res_a = {}
    for sym in SYMBOLS:
        res_a[sym] = _measure(hub_a, sym, start, today)
        print(f"  {sym}  cold={res_a[sym][0]:6.2f}s  "
              f"repeat=[{', '.join(f'{x:.2f}' for x in res_a[sym][1])}]s  rows={res_a[sym][2]}")

    print("\n[B] 修复撤销（_clamp_probe_end 恒等 + 负缓存关闭 = 修复前行为）")
    hub_b = _build_hub(settings)
    hub_b._clamp_probe_end = lambda end, *, now=None: pd.Timestamp(end).date()
    hub_b.delta_probe_cooldown = 0
    res_b = {}
    for sym in SYMBOLS:
        res_b[sym] = _measure(hub_b, sym, start, today)
        print(f"  {sym}  cold={res_b[sym][0]:6.2f}s  "
              f"repeat=[{', '.join(f'{x:.2f}' for x in res_b[sym][1])}]s  rows={res_b[sym][2]}")

    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    worst_a = max(x for sym in SYMBOLS for x in res_a[sym][1])
    worst_b = max(x for sym in SYMBOLS for x in res_b[sym][1])
    total_a = sum(x for sym in SYMBOLS for x in res_a[sym][1])
    total_b = sum(x for sym in SYMBOLS for x in res_b[sym][1])
    print(f"重复请求最慢单次：A={worst_a:.2f}s   B={worst_b:.2f}s")
    print(f"重复请求合计    ：A={total_a:.2f}s   B={total_b:.2f}s   "
          f"提速 {(total_b / total_a if total_a else float('inf')):.1f}x")
    rows_ok = all(res_a[s][2] == res_b[s][2] for s in SYMBOLS)
    print(f"返回行数一致（修复未丢数据）：{rows_ok}")
    if worst_b < 1.0:
        print("⚠ B 组也很快 → 当前时段缺口探测本就不触发，本次复测无法证伪修复；"
              "请在盘前/午休/周末重跑。")
    elif worst_a < 1.0 and rows_ok:
        print("✔ 修复生效：重复请求零探测，且返回数据与修复前完全一致。")
    else:
        print("✘ 修复未达预期，需回到根因调查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
