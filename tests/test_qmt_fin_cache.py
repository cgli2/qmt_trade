"""QMT 财务跨进程磁盘缓存逻辑单测（不依赖真实 QMT / xtquant）。

验证：
1. 首次：缺失 → 触发"下载"（monkeypatch）并落盘 parquet。
2. 二次（清空进程内缓存模拟新进程）：直接读盘，不触发"下载"。
3. TTL 过期（ttl=0）：重新触发"下载"。
4. 落盘文件确实存在且可被读回。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from qmt_trade.datahub.providers.qmt_provider import QmtProvider


def make_raw(syms):
    df = pd.DataFrame({"m_anntime": [20260601], "m_timetag": [20260331],
                       "du_return_on_equity": [0.12]})
    return {s: {"PershareIndex": df, "Income": df.copy()} for s in syms}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="fin_cache_test_"))
    prov = QmtProvider(cache_dir=str(tmp), fundamental_ttl=86400)

    calls = {"n": 0}

    def fake_download(syms, tables, s, e, timeout=180):
        calls["n"] += 1
        return make_raw(syms)

    prov._download_raw = fake_download

    syms = ["600000.SH", "000001.SZ", "300750.SZ"]

    # 1) 首次：全部缺失 → 触发下载 + 落盘
    out1 = prov._ensure_financial_raw(list(syms), "20230101", "20260813", 10)
    assert len(out1) == 3, f"首次应拿到 3 只，实际 {len(out1)}"
    assert calls["n"] == 1, f"首次应下载 1 次，实际 {calls['n']}"
    # 落盘文件应存在
    for s in syms:
        for t in ("PershareIndex", "Income"):
            p = prov._fin_path(s, t)
            assert p.exists(), f"落盘文件缺失: {p}"
    print("[OK] 首次：触发下载并落盘 parquet，文件数 =", len(list(tmp.glob('*.parquet'))))

    # 2) 二次：模拟新进程（清空进程内缓存）→ 应读盘、不触发下载
    prov._fin_cache.clear()
    out2 = prov._ensure_financial_raw(list(syms), "20230101", "20260813", 10)
    assert len(out2) == 3, f"二次应读盘拿到 3 只，实际 {len(out2)}"
    assert calls["n"] == 1, f"二次不应再下载，下载次数仍为 1，实际 {calls['n']}"
    print("[OK] 二次：直接读盘，未触发 QMT 下载（命中跨进程缓存）")

    # 3) TTL 过期（ttl=0 → 永远视为过期）→ 应重新触发下载
    prov._fin_ttl = 0
    prov._fin_cache.clear()
    out3 = prov._ensure_financial_raw(list(syms), "20230101", "20260813", 10)
    assert len(out3) == 3
    assert calls["n"] == 2, f"TTL 过期后应再次下载，下载次数=2，实际 {calls['n']}"
    print("[OK] TTL 过期：重新触发下载（仅新数据才拉）")

    # 4) 部分缺失：只缺 1 只 → 仅下载这 1 只，其余读盘
    prov._fin_ttl = 86400
    prov._fin_cache.clear()
    # 删掉 600000.SH 的缓存，模拟新上市的票没缓存
    for t in ("PershareIndex", "Income"):
        prov._fin_path("600000.SH", t).unlink()
    calls["n"] = 0
    out4 = prov._ensure_financial_raw(list(syms), "20230101", "20260813", 10)
    assert len(out4) == 3, f"部分缺失应补齐到 3 只，实际 {len(out4)}"
    assert calls["n"] == 1, f"仅缺失 1 只应下载 1 次，实际 {calls['n']}"
    print("[OK] 部分缺失：仅下载缺失标的，已缓存的复用")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
