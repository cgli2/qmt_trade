"""取证：盘中「日线当日 bar」是否会随行情推进刷新（性能修复 2026-09-16 副作用排查）。

为什么要查这个
--------------
日线走范围感知缓存（key 不含 end，内存 TTL=86400s，磁盘 TTL=12h）。增量补齐分支的
前置条件是 ``cached_end < end``：盘中缓存末端**已经等于今天**，该条件为假 →
``probe_end = None`` → 不探测。于是当日那根 bar 可能被冻结在「首次取数的时刻」，
直到内存 TTL 过期（24h）或被 LRU 淘汰。

本次修复把日历收敛收窄成「仅 CLOSED/PRE_OPEN」正是为了**不加重**这个问题，但收窄
本身并不能让当日 bar 主动刷新 —— 那是既有缓存语义决定的，需要单独取证确认严重性。

判据（用 /market/quote 作 oracle：tick TTL=0，永不缓存，一定是实时值）
--------------------------------------------------------------------
成交量在盘中单调不减，是最强的新鲜度信号：
  * kline 当日 volume ≈ quote volume（允许 1x/100x 单位差） → 新鲜；
  * kline 当日 volume 明显 < quote volume，且 sleep 后 kline 不变而 quote 增长
    → **当日 bar 已冻结**，用户看到的是几十分钟前的价格。

本脚本只读 HTTP 接口，不重启、不干扰常驻后端。
运行：python tests/diag_daily_bar_freshness.py
（遵守项目规则：所有测试用 Python 脚本，禁用 curl。）
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# Windows GBK 控制台编不出 ✔/✘/≈，统一切 UTF-8（否则 UnicodeEncodeError 打断脚本）
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:5074/api"
TIMEOUT = 120
SLEEP_SECONDS = 45          # 足够让成交量产生可见增量，又不至于拖太久
N_SYMBOLS = 3


def _get(path: str, params: dict | None = None):
    url = f"{BASE}{path}"
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        url = f"{url}?{qs}"
    t0 = time.perf_counter()
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        return (time.perf_counter() - t0), None, f"HTTP {e.code}"
    except Exception as e:                                        # noqa: BLE001
        return (time.perf_counter() - t0), None, f"{type(e).__name__}: {e}"
    try:
        return (time.perf_counter() - t0), json.loads(body), None
    except Exception as e:                                        # noqa: BLE001
        return (time.perf_counter() - t0), None, f"decode: {e}"


def _kline_last(symbol: str) -> tuple[dict | None, float, str | None]:
    """返回 (当日 bar dict, 耗时, 错误)。取 D1 区间末根。"""
    start = (date.today() - timedelta(days=200)).isoformat()
    el, body, err = _get("/market/kline", {
        "symbol": symbol, "period": "D1", "start": start,
        "adjust": "QFQ", "limit": 400,
    })
    if err or not body:
        return None, el, err or "empty body"
    rows = body.get("rows") or []
    return (rows[-1] if rows else None), el, None


def _quotes(symbols: list[str]) -> tuple[dict, float, str | None]:
    el, body, err = _get("/market/quote", {"symbols": ",".join(symbols)})
    if err or not body:
        return {}, el, err or "empty body"
    return (body.get("quotes") or {}), el, None


def _align_volume(v_kline: float, v_quote: float) -> float:
    """把 kline 成交量对齐到 quote 的量纲，返回对齐后的比值（kline/quote）。

    QMT 的 volume 以「手」计、东财实时以「股」计，比值可能是 1 或 1/100。
    取更接近 1 的那个量纲，避免把单位差误判成"数据冻结"。
    """
    if not v_quote:
        return float("nan")
    raw = v_kline / v_quote
    return raw * 100.0 if raw < 0.02 else raw


def main() -> int:
    today = date.today()
    print("=" * 78)
    print(f"now = {time.strftime('%Y-%m-%d %H:%M:%S')}   today = {today}")
    print(f"oracle = /market/quote（tick TTL=0，永不缓存）   间隔 = {SLEEP_SECONDS}s")
    print("=" * 78)

    # ---- 0. 取样标的：用策略推荐列表，与用户报障路径一致 ----
    el, body, err = _get("/selection/picks", {"mode": "paper"})
    if err or not body:
        print(f"无法取策略推荐列表：{err}")
        return 1
    picks = [r.get("symbol") for r in (body.get("picks") or []) if r.get("symbol")]
    if not picks:                                  # 兜底：结构不符时用固定样本
        picks = ["001256.SZ", "301033.SZ", "301190.SZ"]
        print("  [warn] picks 结构与预期不符，改用固定样本")
    syms = picks[:N_SYMBOLS]
    print(f"\n[0] picks={len(picks)} 条   样本 = {syms}   （/selection/picks {el:.2f}s）")

    # ---- 1. 第一轮：kline 当日 bar vs quote 实时值 ----
    print(f"\n[1] 第一轮对比（kline 当日 bar  vs  quote 实时）")
    q0, qel, qerr = _quotes(syms)
    if qerr:
        print(f"  quote 失败：{qerr}")
        return 1
    round1: dict[str, dict] = {}
    print(f"  {'symbol':<12}{'k_date':<12}{'k_close':>9}{'q_last':>9}"
          f"{'k_vol':>14}{'q_vol':>14}{'vol比':>8}  {'kline耗时':>9}")
    for s in syms:
        bar, kel, kerr = _kline_last(s)
        q = q0.get(s) or {}
        if kerr or bar is None:
            print(f"  {s:<12}kline 失败：{kerr}")
            continue
        k_date = str(bar.get("date"))[:10]
        k_close = float(bar.get("close") or 0)
        k_vol = float(bar.get("volume") or 0)
        q_last = float(q.get("last") or 0)
        q_vol = float(q.get("volume") or 0)
        ratio = _align_volume(k_vol, q_vol)
        round1[s] = {"date": k_date, "close": k_close, "vol": k_vol,
                     "q_last": q_last, "q_vol": q_vol, "ratio": ratio, "el": kel}
        flag = "" if k_date == today.isoformat() else "  ← 末根不是今天！"
        print(f"  {s:<12}{k_date:<12}{k_close:>9.2f}{q_last:>9.2f}"
              f"{k_vol:>14,.0f}{q_vol:>14,.0f}{ratio:>8.3f}  {kel:>8.2f}s{flag}")

    if not round1:
        print("  无可用样本，终止")
        return 1

    # ---- 2. 等待，让真实成交量产生增量 ----
    print(f"\n[2] sleep {SLEEP_SECONDS}s，让盘中成交量推进…")
    time.sleep(SLEEP_SECONDS)

    # ---- 3. 第二轮：quote 必然增长；kline 是否跟着动？ ----
    print(f"\n[3] 第二轮对比")
    q1, qel, qerr = _quotes(syms)
    if qerr:
        print(f"  quote 失败：{qerr}")
        return 1
    print(f"  {'symbol':<12}{'q_vol增量':>14}{'k_vol变化':>14}"
          f"{'k_close→':>10}{'q_last→':>10}{'vol比':>8}  {'判定':<12}")
    stale: list[str] = []
    fresh: list[str] = []
    for s, r1 in round1.items():
        bar, kel, kerr = _kline_last(s)
        q = q1.get(s) or {}
        if kerr or bar is None:
            print(f"  {s:<12}kline 失败：{kerr}")
            continue
        k_vol2 = float(bar.get("volume") or 0)
        k_close2 = float(bar.get("close") or 0)
        q_last2 = float(q.get("last") or 0)
        q_vol2 = float(q.get("volume") or 0)
        d_qvol = q_vol2 - r1["q_vol"]
        d_kvol = k_vol2 - r1["vol"]
        ratio2 = _align_volume(k_vol2, q_vol2)
        # quote 成交量确实增长了（证明市场在动、oracle 是活的），而 kline 当日 bar 纹丝不动
        is_stale = d_qvol > 0 and d_kvol == 0 and abs(ratio2 - 1.0) > 0.02
        (stale if is_stale else fresh).append(s)
        verdict = "✘ 已冻结" if is_stale else "✔ 新鲜"
        print(f"  {s:<12}{d_qvol:>14,.0f}{d_kvol:>14,.0f}"
              f"{r1['close']:>6.2f}→{k_close2:<4.2f}{q_last2:>9.2f}"
              f"{ratio2:>8.3f}  {verdict:<12}({kel:.2f}s)")

    # ---- 4. /market/symbols 是否持续慢 ----
    print(f"\n[4] /market/symbols 连续 3 次耗时（判断 1.6s 是冷启动还是常态）")
    for i in range(3):
        el, body, err = _get("/market/symbols")
        n = len((body or {}).get("symbols") or []) if body else 0
        print(f"  #{i + 1}  {el:6.2f}s  symbols={n}  {err or ''}")

    # ---- 5. 结论 ----
    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    if stale:
        print(f"✘ 当日日线 bar 在盘中被冻结：{stale}")
        print("  影响：K线图最后一根（今天）显示的是首次取数时刻的价格/成交量，"
              "盘中不会更新，直到内存 TTL(24h) 过期或被 LRU 淘汰。")
        print("  成因：日线范围感知缓存 key 不含 end + 增量分支前置条件 cached_end < end，"
              "盘中 cached_end == end 故不探测。属既有缓存语义，非本次修复引入。")
    else:
        print(f"✔ 当日日线 bar 盘中会刷新（新鲜样本 {len(fresh)} 只），无需额外处理。")
        if fresh:
            print("  说明增量分支或缓存失效路径确实在盘中重新取到了当日数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
