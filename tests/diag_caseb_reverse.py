"""反向验证 case-B 守护测试的有效性（一次性诊断脚本，遵守项目规则：只用 Python，禁 curl）。

把 DataHub._today_refresh_due 猴补成恒 False —— 等价于「撤销当日 bar 刷新分支」：
增量分支里 ``elif self._today_refresh_due(...)`` 恒假 → 落到 else → probe=None → 不重探。
预期：核心/节流/写穿三条**变红**（证明能抓到 bug），历史 end no-op **保持绿**
（case-B 对回测本就无副作用，_today_refresh_due 第一道判据 end!=today 就返回 False）。
跑完即还原猴补，绝不改动生产代码。

运行：python tests/diag_caseb_reverse.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
for p in (str(ROOT), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

logging.disable(logging.WARNING)  # 屏蔽 GBK 控制台乱码的告警噪声

from qmt_trade.datahub.manager import DataHub  # noqa: E402
import test_delta_probe_guard as T  # noqa: E402

# (测试名, 撤销 case-B 后是否**期望变红**)
CASE_B_TESTS = [
    ("test_today_bar_refreshes_when_stale", True),
    ("test_today_bar_refresh_throttled_by_ttl", True),
    ("test_today_bar_refresh_writes_through_to_disk", True),
    ("test_today_bar_no_refresh_for_historical_end", False),  # no-op，须保持绿
]


def _run(fn):
    """跑单个测试：返回 (是否通过, 首行错误摘要)。"""
    try:
        fn()
        return True, None
    except AssertionError as e:
        return False, (str(e).splitlines() or ["AssertionError"])[0][:110]
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    orig = DataHub._today_refresh_due

    print("== baseline（case-B 正常，应全绿）==")
    base = {}
    for name, _ in CASE_B_TESTS:
        ok, err = _run(getattr(T, name))
        base[name] = ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  <- {err}" if err else ""))

    print("\n== reverted（撤销 case-B：_today_refresh_due → 恒 False）==")
    DataHub._today_refresh_due = lambda self, *a, **k: False
    rev = {}
    try:
        for name, expect_red in CASE_B_TESTS:
            ok, err = _run(getattr(T, name))
            rev[name] = ok
            want = "应变红" if expect_red else "应保持绿"
            good = (not ok) if expect_red else ok
            print(f"  {'PASS(绿)' if ok else 'FAIL(红)'}  {name}  [{want} -> "
                  f"{'符合' if good else '!!不符合'}]" + (f"  <- {err}" if err else ""))
    finally:
        DataHub._today_refresh_due = orig  # 还原猴补

    base_all_green = all(base.values())
    core_red = all(not rev[n] for n, e in CASE_B_TESTS if e)
    hist_green = rev["test_today_bar_no_refresh_for_historical_end"]
    ok = base_all_green and core_red and hist_green

    print("\n== 结论 ==")
    print(f"  baseline 全绿                       : {base_all_green}")
    print(f"  撤销后 核心/节流/写穿 三条变红      : {core_red}")
    print(f"  撤销后 历史 end no-op 保持绿        : {hist_green}")
    print(f"  => 反向验证{'通过' if ok else '失败'}："
          f"守护测试{'确实能抓到 case-B bug' if ok else '无法抓到 bug，测试无效'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
