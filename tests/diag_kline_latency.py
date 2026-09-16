"""诊断：/market/kline 从「策略推荐」点击到 K 线返回的真实耗时分布。

目标（systematic-debugging Phase 1 证据收集）：
1. 首次请求 vs 二次请求（同 symbol/period）耗时差 → 判定是否被缓存覆盖；
2. D1/W1/M1/Y1 各周期耗时 → 判定 _WARMUP_DAYS 的影响；
3. 多 symbol 连续切换耗时 → 复现「切换其他个股也明显卡顿」；
4. 直接调 DataHub.get_bars 分段计时 → 定位磁盘缓存 / 增量补齐分支。

用法：python tests/diag_kline_latency.py
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

BASE = "http://127.0.0.1:5074/api"
TIMEOUT = 120


def _get(path: str, params: dict | None = None):
    url = f"{BASE}{path}"
    if params:
        qs = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")}
        )
        url = f"{url}?{qs}"
    t0 = time.perf_counter()
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:  # noqa: PERF203
        return (time.perf_counter() - t0), None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return (time.perf_counter() - t0), None, f"{type(e).__name__}: {e}"
    try:
        return (time.perf_counter() - t0), json.loads(body), None
    except Exception as e:  # noqa: BLE001
        return (time.perf_counter() - t0), None, f"decode: {e}"


def default_start(p: str) -> str:
    """复刻前端 MarketView.defaultStartFor 的回看区间。"""
    months = {"D1": 6, "W1": 24, "M1": 96, "Y1": 360}
    d = date.today()
    m = d.month - months.get(p, 6)
    y = d.year
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}-{min(d.day, 28):02d}"


def main() -> None:
    print("=" * 78)
    print("A. 取「策略推荐」标的列表")
    el, picks, err = _get("/selection/picks", {"mode": "paper"})
    if err:
        print(f"   /selection/picks 失败: {err} ({el:.2f}s)")
        syms = ["600000.SH", "000001.SZ", "600519.SH", "300750.SZ"]
        print(f"   退回固定样本: {syms}")
    else:
        el2, _, _ = (el, None, None)
        rows = (picks or {}).get("picks") or []
        syms = [r.get("symbol") for r in rows if r.get("symbol")][:6]
        print(f"   picks={len((picks or {}).get('picks') or [])} 条, 耗时 {el:.2f}s")
        print(f"   样本: {syms}")
    if not syms:
        syms = ["600000.SH", "000001.SZ"]

    print()
    print("B. 各周期首次 vs 二次（同一标的）")
    print(f"   {'period':<7}{'start':<13}{'首次(s)':>10}{'rows':>7}"
          f"{'二次(s)':>10}{'三次(s)':>10}")
    s0 = syms[0]
    for p in ("D1", "W1", "M1", "Y1"):
        st = default_start(p)
        t1, r1, e1 = _get("/market/kline", {
            "symbol": s0, "period": p, "start": st, "mode": "paper",
            "limit": 400, "adjust": "QFQ"})
        t2, r2, e2 = _get("/market/kline", {
            "symbol": s0, "period": p, "start": st, "mode": "paper",
            "limit": 400, "adjust": "QFQ"})
        t3, r3, e3 = _get("/market/kline", {
            "symbol": s0, "period": p, "start": st, "mode": "paper",
            "limit": 400, "adjust": "QFQ"})
        n = len((r1 or {}).get("rows") or [])
        note = " ".join(x for x in (e1, e2, e3) if x)
        print(f"   {p:<7}{st:<13}{t1:>10.2f}{n:>7}{t2:>10.2f}{t3:>10.2f}  {note}")

    print()
    print("C. 连续切换标的（D1，模拟用户点击不同个股）")
    st = default_start("D1")
    print(f"   {'symbol':<13}{'首次(s)':>10}{'二次(s)':>10}{'rows':>7}")
    tot = 0.0
    for s in syms:
        t1, r1, e1 = _get("/market/kline", {
            "symbol": s, "period": "D1", "start": st, "mode": "paper",
            "limit": 400, "adjust": "QFQ"})
        t2, r2, e2 = _get("/market/kline", {
            "symbol": s, "period": "D1", "start": st, "mode": "paper",
            "limit": 400, "adjust": "QFQ"})
        n = len((r1 or {}).get("rows") or [])
        tot += t1
        print(f"   {s:<13}{t1:>10.2f}{t2:>10.2f}{n:>7}"
              f"  {' '.join(x for x in (e1, e2) if x)}")
    print(f"   首次合计 {tot:.2f}s / {len(syms)} 只 = 平均 {tot / max(1, len(syms)):.2f}s")

    print()
    print("D. 并发 4 个不同标的（模拟快速切换，看是否串行化）")
    import concurrent.futures as cf
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(_get, "/market/kline", {
            "symbol": s, "period": "D1", "start": st, "mode": "paper",
            "limit": 400, "adjust": "QFQ"}) for s in syms[:4]]
        for f in cf.as_completed(futs):
            el, _, e = f.result()
            print(f"   单请求 {el:.2f}s {e or ''}")
    print(f"   墙钟总耗时 {time.perf_counter() - t0:.2f}s")

    print()
    print("E. 其他页面级请求耗时（判定是否 symbols/picks 也在拖后腿）")
    for path, prm in (("/market/symbols", {"mode": "paper"}),
                      ("/selection/picks", {"mode": "paper"}),
                      ("/market/timeline", {"symbol": s0, "mode": "paper"})):
        el, r, e = _get(path, prm)
        print(f"   {path:<24}{el:>8.2f}s  {e or ''}")

    print("=" * 78)


if __name__ == "__main__":
    main()
