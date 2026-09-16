"""分层反向验证：把修复逐层还原，确认 test_delta_probe_guard.py 真的会**变红**。

绿灯本身不证明测试有效 —— 一个恒真断言也能全绿。本项目第一版测试就曾出现
「还原修复后依然 PASS」的无效用例（自我验证循环 + 被负缓存掩盖）。因此每次
改动测试或生产代码后，都必须跑一遍本脚本：

  REVERSE-1  还原成**第一版过宽收敛**（非 POST_CLOSE 一律排除当日）
             → 盘中回归守护必须变红，盘前守护必须仍绿
  REVERSE-2  完全撤销层 1（_clamp_probe_end 退化为恒等）
             → 盘前守护必须变红，盘中守护必须仍绿
  REVERSE-3  完全撤销层 2（delta_probe_cooldown 强制 0）
             → 负缓存守护必须变红

三组全部通过 monkeypatch 完成，**不修改任何生产文件**，也不需要重启后端。

运行：python tests/diag_reverse_verify.py
（遵守项目规则：所有测试用 Python 脚本，禁用 curl。）
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，编不出 ✔/✘ 会 UnicodeEncodeError 直接打断脚本
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

logging.disable(logging.WARNING)          # 屏蔽 GBK 控制台下的乱码 warning

import qmt_trade.datahub.manager as mgr                   # noqa: E402
from qmt_trade.core.clock import Session, TradingCalendar  # noqa: E402
from qmt_trade.datahub.manager import DataHub              # noqa: E402

TEST_FILE = ROOT / "tests" / "test_delta_probe_guard.py"
_CAL = TradingCalendar()


def _load_test_module():
    """按路径加载被测测试模块（文件名非 test_* 可导入形式，用 spec 装载）。"""
    spec = importlib.util.spec_from_file_location("_tdpg_under_test", TEST_FILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)          # noqa: E402
    return mod


def _run_all(mod) -> dict[str, str]:
    """跑模块内全部 test_*，返回 {name: "PASS"|"FAIL"}（不抛异常）。"""
    out: dict[str, str] = {}
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        fn = getattr(mod, name)
        if not callable(fn):
            continue
        try:
            fn()
            out[name] = "PASS"
        except BaseException:                            # noqa: BLE001
            out[name] = "FAIL"
    return out


# --------------------------------------------------------------- 三套还原补丁
def _clamp_v1_overwide(self, end, *, now: datetime | None = None):
    """第一版（**有回归**）：除 POST_CLOSE 外一律把上界压到上一交易日。"""
    try:
        end_d = pd.Timestamp(end).date()
    except (TypeError, ValueError):
        return None
    try:
        # ★ 必须走 mgr.datetime，否则测试的 _frozen_now() 冻结不到本函数，
        #   REVERSE-1 会用真实当前时间 → 还原失效、反向验证给出假的「符合预期」。
        now = now or mgr.datetime.now()
        today = now.date()
        last_closed = (_CAL.align_to_trading_day(today)
                       if _CAL.session_of(now) is Session.POST_CLOSE
                       else _CAL.prev_trading_day(today))
    except Exception:                                    # noqa: BLE001
        return end_d
    return min(end_d, last_closed)


def _clamp_identity(self, end, *, now: datetime | None = None):
    """完全撤销层 1：原样返回请求 end（修复前行为）。"""
    try:
        return pd.Timestamp(end).date()
    except (TypeError, ValueError):
        return None


def _apply(mod, reverse_id: str):
    """打补丁；返回撤销函数。"""
    orig_clamp = DataHub._clamp_probe_end
    orig_make_hub = mod._make_hub

    if reverse_id == "REVERSE-1":
        DataHub._clamp_probe_end = _clamp_v1_overwide
    elif reverse_id == "REVERSE-2":
        DataHub._clamp_probe_end = _clamp_identity
    elif reverse_id == "REVERSE-3":
        def _hub_no_cooldown(tmp, provider, cooldown=60):
            hub = orig_make_hub(tmp, provider, cooldown=cooldown)
            hub.delta_probe_cooldown = 0                 # 撤销层 2
            return hub
        mod._make_hub = _hub_no_cooldown
    else:
        raise ValueError(reverse_id)

    def _restore():
        DataHub._clamp_probe_end = orig_clamp
        mod._make_hub = orig_make_hub
    return _restore


# ------------------------------------------------------- 每组「必须红/必须绿」
EXPECT = {
    "REVERSE-1": {
        "desc": "还原第一版过宽收敛（盘中当日 bar 被挡掉）",
        "must_red": {
            "test_clamp_by_session",
            "test_clamp_matches_calendar_oracle",
            "test_lunch_and_auction_do_not_clamp",
            "test_intraday_probes_today_bar",
        },
        "must_green": {
            # v1 对盘前同样收敛 → 盘前守护不该被这次还原影响
            "test_no_delta_probe_premarket",
            "test_no_delta_probe_across_symbols_premarket",
            "test_postclose_still_probes_when_data_published",
            "test_clamp_is_noop_for_historical_end",
            "test_negative_cache_suppresses_repeat_probe",
            "test_cooldown_zero_disables_negative_cache",
            "test_probe_success_clears_negative_entry",
        },
    },
    "REVERSE-2": {
        "desc": "完全撤销层 1（_clamp_probe_end 恒等 = 修复前）",
        "must_red": {
            "test_clamp_by_session",
            "test_clamp_matches_calendar_oracle",
            "test_no_delta_probe_premarket",
            "test_no_delta_probe_across_symbols_premarket",
        },
        "must_green": {
            # 恒等收敛对盘中无害 → 盘中守护必须仍绿，否则说明它们测的不是层 1
            "test_lunch_and_auction_do_not_clamp",
            "test_intraday_probes_today_bar",
            "test_postclose_still_probes_when_data_published",
            "test_clamp_is_noop_for_historical_end",
            "test_negative_cache_suppresses_repeat_probe",
            "test_cooldown_zero_disables_negative_cache",
            "test_probe_success_clears_negative_entry",
        },
    },
    "REVERSE-3": {
        "desc": "完全撤销层 2（负缓存冷却强制为 0）",
        "must_red": {
            "test_negative_cache_suppresses_repeat_probe",
        },
        "must_green": {
            "test_clamp_by_session",
            "test_clamp_matches_calendar_oracle",
            "test_clamp_is_noop_for_historical_end",
            "test_lunch_and_auction_do_not_clamp",
            "test_intraday_probes_today_bar",
            "test_no_delta_probe_premarket",
            "test_no_delta_probe_across_symbols_premarket",
            "test_postclose_still_probes_when_data_published",
            "test_cooldown_zero_disables_negative_cache",
            "test_probe_success_clears_negative_entry",
        },
    },
}


def main() -> int:
    mod = _load_test_module()
    baseline = _run_all(mod)
    print("=" * 78)
    print(f"[BASELINE] 未打补丁：{sum(v == 'PASS' for v in baseline.values())}"
          f"/{len(baseline)} PASS")
    base_red = sorted(k for k, v in baseline.items() if v != "PASS")
    if base_red:
        print(f"  ✘ 基线就有失败，反向验证无意义：{base_red}")
        return 1
    print("  ✔ 基线全绿，可以开始反向验证")

    overall_ok = True
    for rid, spec in EXPECT.items():
        restore = _apply(mod, rid)
        try:
            res = _run_all(mod)
        finally:
            restore()

        red = {k for k, v in res.items() if v != "PASS"}
        missed = sorted(spec["must_red"] - red)         # 该红却没红 = 无效测试
        broken = sorted(spec["must_green"] & red)       # 不该红却红了 = 过度耦合
        ok = not missed and not broken
        overall_ok &= ok

        print("\n" + "=" * 78)
        print(f"[{rid}] {spec['desc']}")
        print(f"  实际变红 {len(red)} 条：{sorted(red)}")
        print(f"  {'✔' if ok else '✘'} 判定：{'符合预期' if ok else '不符合预期'}")
        if missed:
            print(f"  ✘ 该红却没红（测试无效，抓不到这层 bug）：{missed}")
        if broken:
            print(f"  ✘ 不该红却红了（测试与本层过度耦合）：{broken}")

    # 还原后再跑一次，确认 monkeypatch 没有污染
    after = _run_all(mod)
    still_red = sorted(k for k, v in after.items() if v != "PASS")
    print("\n" + "=" * 78)
    if still_red:
        print(f"✘ 还原后仍有失败，补丁未干净撤销：{still_red}")
        overall_ok = False
    else:
        print(f"✔ 还原后 {len(after)}/{len(after)} 全绿，monkeypatch 已干净撤销")

    print("\n" + ("✔ 三层反向验证全部通过：测试确实能抓到每一层的缺失"
                  if overall_ok else "✘ 反向验证未通过，测试需要修正"))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
