"""全量标的池(universe) TTL 缓存回归测试。

背景：WebUI 交易页每次点「模拟盘/实盘」tab 都会经 /market/symbols 空参请求全市场
标的（akshare 全量快照 / QMT 逐只 detail，1~5s+），曾导致 tab 点击卡顿。修复给
DataHub.get_instruments() 的「全量 + 实盘模式(asof is None)」路径加了 TTL 缓存。

本测试锁定该修复的行为契约（纯 Python 单元级验证，不经 HTTP，不用 curl）：
  1. asof=None 时重复全量请求命中 TTL 缓存，只 dispatch 一次；
  2. set_asof(日期) 后旁路缓存重新 dispatch（回测 PIT 绝不复用，防前视偏差）；
  3. universe_ttl=0 时禁用缓存（配置逃生阀）；
  4. 源返回空时不写缓存（不把空名单锁定一个 TTL 周期）；
  5. 指定标的(per-symbol)路径仍走既有 _instrument_cache，无回归；
  6. TTL 过期后重新 dispatch（缓存不会永久陈旧）。

运行：python tests/test_universe_cache.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 隔离 runtime duckdb：DataHub 构造时会经 DuckDBStore 打开它，若指向生产
# data/db/qmt.duckdb 会与正在运行的后端争锁。用独立临时文件避免冲突。
_TMP = tempfile.mkdtemp(prefix="universe_cache_test_")
os.environ["QMT_RUNTIME_DB"] = os.path.join(_TMP, "runtime.duckdb")

from qmt_trade.core.config import get_settings  # noqa: E402
from qmt_trade.core.errors import DataUnavailableError  # noqa: E402
from qmt_trade.core.instruments import normalize_symbol  # noqa: E402
from qmt_trade.datahub.manager import DataHub  # noqa: E402
from qmt_trade.datahub.providers.base import Capability, DataProvider  # noqa: E402
from qmt_trade.datahub.types import InstrumentInfo  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def _infos() -> list[InstrumentInfo]:
    """构造 3 只已归一化的假标的（symbol 保持 .SH/.SZ 后缀，与 _sym_list 归一化一致）。"""
    return [
        InstrumentInfo(symbol="600000.SH", name="浦发银行"),
        InstrumentInfo(symbol="000001.SZ", name="平安银行"),
        InstrumentInfo(symbol="300750.SZ", name="宁德时代"),
    ]


class CountingInstrumentsProvider(DataProvider):
    """只支持 INSTRUMENTS、并记录 get_instruments 调用次数的计数型假源。

    通过 self.calls 精确断言 DataHub 是否真的向数据源 dispatch，从而验证
    「命中缓存 = 不再打扰数据源」这一核心行为。
    """

    name = "counter"
    capabilities = {Capability.INSTRUMENTS}

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0
        self.result: list[InstrumentInfo] = _infos()

    def is_available(self) -> bool:
        return True

    def get_instruments(self, symbols=None) -> list[InstrumentInfo]:
        self.calls += 1
        if symbols:
            want = {normalize_symbol(s) for s in symbols}
            return [i for i in self.result if i.symbol in want]
        return list(self.result)


def main() -> int:
    st = get_settings()
    st.set("datahub.priority.instruments", ["counter"])
    counter = CountingInstrumentsProvider()
    hub = DataHub(st, [counter])

    def reset(*, ttl: float = 1800.0) -> None:
        """复用同一个 DataHub（避免多次打开 runtime duckdb），逐场景清空缓存状态。"""
        hub.set_asof(None)
        hub.universe_ttl = ttl
        hub._universe_cache = None
        hub._universe_cache_at = 0.0
        hub._instrument_cache.clear()
        counter.calls = 0
        counter.result = _infos()

    print("\n[1] asof=None：重复全量请求命中 TTL 缓存，只 dispatch 一次")
    reset()
    r1 = hub.get_instruments()
    check("首次 dispatch 一次", counter.calls == 1, f"calls={counter.calls}")
    check("返回全部标的", len(r1) == 3, f"n={len(r1)}")
    r2 = hub.get_instruments()
    check("二次命中缓存不再 dispatch", counter.calls == 1, f"calls={counter.calls}")
    check("命中返回同一份缓存对象", r2 is r1)

    print("\n[2] 回测 PIT：set_asof(日期) 旁路缓存重新 dispatch，且切回实盘仍命中原缓存")
    reset()
    hub.get_instruments()                       # 预热实盘缓存 (calls=1)
    hub.set_asof(date(2026, 3, 31))
    hub.get_instruments()                       # PIT 必须重新取，绝不复用实盘缓存
    check("PIT 模式旁路缓存重新 dispatch", counter.calls == 2, f"calls={counter.calls}")
    hub.set_asof(None)
    hub.get_instruments()                       # 切实盘：原缓存未被 PIT 调用污染
    check("切回实盘仍命中原缓存(未被 PIT 覆盖)", counter.calls == 2, f"calls={counter.calls}")

    print("\n[3] universe_ttl=0：禁用缓存，每次都 dispatch")
    reset(ttl=0.0)
    hub.get_instruments()
    hub.get_instruments()
    check("ttl=0 时两次都 dispatch", counter.calls == 2, f"calls={counter.calls}")

    print("\n[4] 源返回空：不写缓存（避免把空名单锁定一个 TTL 周期）")
    reset()
    counter.result = []
    try:
        hub.get_instruments()
        raised = False
    except DataUnavailableError:
        raised = True
    check("空结果按契约抛 DataUnavailableError", raised)
    check("空结果未写入 universe 缓存", hub._universe_cache is None)
    counter.result = _infos()                   # 源恢复后应能正常缓存
    got = hub.get_instruments()
    check("源恢复后正常返回并缓存", len(got) == 3 and hub._universe_cache is not None)

    print("\n[5] 指定标的(per-symbol)：仍走既有 _instrument_cache，无回归")
    reset()
    hub.get_instruments()                       # 全量预热，填充 per-symbol 缓存 (calls=1)
    one = hub.get_instruments(["600000.SH"])
    check("指定标的命中 per-symbol 缓存不再 dispatch", counter.calls == 1, f"calls={counter.calls}")
    check("指定标的返回正确", len(one) == 1 and one[0].symbol == "600000.SH")

    print("\n[6] TTL 过期：重新 dispatch，缓存不会永久陈旧")
    reset(ttl=30.0)
    hub.get_instruments()                       # calls=1，写入缓存
    hub._universe_cache_at = time.time() - 31.0  # 人为回拨到 TTL 之外
    hub.get_instruments()
    check("过期后重新 dispatch", counter.calls == 2, f"calls={counter.calls}")

    print(f"\n==== universe 缓存回归：PASS={PASS} FAIL={FAIL} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
