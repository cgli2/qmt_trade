#!/usr/bin/env python
"""QMT 基本面/资金流取数自检（Phase 2，2026-08-12）—— 在你本机跑一次，校准字段映射。

背景：QmtProvider.get_fundamentals 用 xtdata.get_financial_data（PershareIndex+Income 表）
映射到系统的 Fundamental。字段名基于 xtdata.md 文档推断（du_return_on_equity 等），
实际列名若与文档有出入，本脚本会直接打印出来，便于校准。

⚠️ 安全护栏：QMT 下载接口（download_financial_data2 / download_history_data2）在账户
未授权对应数据时会出现"连接在线但数据迟迟不到"的无限阻塞。本脚本对每个下载都加
硬性超时（默认财务 120s / 资金流 90s，可用环境变量 QMT_DIAG_DL_TIMEOUT 覆盖），
超时即放弃并明确提示，绝不整夜卡死。

用法：
    python scripts/diag_qmt_fundamentals.py
    python scripts/diag_qmt_fundamentals.py --moneyflow
    QMT_DIAG_DL_TIMEOUT=300 python scripts/diag_qmt_fundamentals.py --moneyflow
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402


def _run_in_thread(fn, timeout, label):
    """在守护线程跑阻塞调用；超时返回 None。"""
    box: dict = {}
    done = threading.Event()

    def runner():
        try:
            box["r"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["e"] = exc
        finally:
            done.set()

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    if not done.wait(timeout):
        print(f"  [超时] {label} 取数超过 {timeout}s 未完成 —— 放弃（很可能该数据在您账户未授权）。")
        return None
    if "e" in box:
        print(f"  [异常] {label} 取数失败: {box['e']}")
        return None
    return box.get("r")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="QMT 基本面/资金流取数自检")
    ap.add_argument("--symbols", default="600519.SH,000858.SZ,300750.SZ",
                    help="逗号分隔的标的（默认茅台/五粮液/宁德时代）")
    ap.add_argument("--moneyflow", action="store_true",
                    help="同时自检资金流（transactioncount1d 列名与解析）")
    ap.add_argument("--fin-timeout", type=int,
                    default=int(os.environ.get("QMT_DIAG_DL_TIMEOUT", 120)),
                    help="财务下载超时秒数（默认 120）")
    ap.add_argument("--mf-timeout", type=int,
                    default=int(os.environ.get("QMT_DIAG_DL_TIMEOUT", 90)),
                    help="资金流下载超时秒数（默认 90）")
    args = ap.parse_args(argv)

    try:
        from qmt_trade.datahub.providers.qmt_provider import QmtProvider
        from qmt_trade.datahub.types import Fundamental
    except ImportError as exc:
        print(f"无法导入 qmt_trade：{exc}")
        return 1

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    p = QmtProvider()
    # 让通过 DataHub 的正式路径（步骤2）也受 diag 超时约束
    p._dl_timeout = min(args.fin_timeout, 180)
    print(f"QMT 可用: {p.is_available()}")
    if not p.is_available():
        print("提示：需要本机已安装 xtquant（miniQMT 客户端），且行情终端在运行；")
        print("      QMT_MINI_PATH 仅实盘下单网关需要，行情/财务/资金流不需要。")
        return 1

    xt = p.xtdata

    print("\n=== 1) 原始财务表列名（校准字段映射）===")
    tables = ["PershareIndex", "Income"]
    fs, fe = "20240101", "20261231"
    print(f"  发起财务下载（异步，超时 {args.fin_timeout}s）...", flush=True)

    def fin_worker():
        xt.download_financial_data2(syms, tables, fs, fe, None)
        deadline = time.time() + args.fin_timeout
        while time.time() < deadline:
            raw = xt.get_financial_data(syms, tables, fs, fe, "announce_time")
            ok = False
            for sym_d in (raw or {}).values():
                for t in tables:
                    df = (sym_d or {}).get(t)
                    if df is not None and not getattr(df, "empty", True):
                        ok = True
                        break
                if ok:
                    break
            if ok:
                return raw
            time.sleep(0.5)
        return None

    raw = _run_in_thread(fin_worker, args.fin_timeout + 10, "财务")
    if not raw:
        print("  QMT 财务下载未就绪/超时。真实回测中将自动降级到 akshare/tushare，")
        print("  不会卡死；但无法用 QMT 校准财务字段。若确需 QMT 财务，请确认")
        print("  miniQMT 账户已开通财务数据权限。")
    else:
        for sym in syms:
            per = (raw.get(sym) or {}).get("PershareIndex")
            inc = (raw.get(sym) or {}).get("Income")
            print(f"\n[{sym}] PershareIndex 列: "
                  f"{sorted(per.columns) if per is not None and not per.empty else '空'}")
            print(f"[{sym}] Income 列: "
                  f"{sorted(inc.columns) if inc is not None and not inc.empty else '空'}")

    print("\n=== 2) 映射后的 Fundamental（走系统正式路径）===")
    try:
        from qmt_trade.core.config import get_settings
        from qmt_trade.datahub.manager import DataHub
        from qmt_trade.datahub.providers.mock import MockProvider
        st = get_settings()
        # QMT 不可用时自动降级 akshare（兜底源），避免报错中断
        st.set("datahub.priority.fundamentals", ["qmt", "akshare"])
        for cat in ("bars", "instruments", "news", "events"):
            st.set(f"datahub.priority.{cat}", ["mock"])
        providers = [p, MockProvider(n_symbols=10, start="2025-01-02", end="2026-08-07")]
        try:
            from qmt_trade.datahub.providers.akshare_provider import AkshareProvider
            providers.append(AkshareProvider())
        except Exception:  # noqa: BLE001
            pass
        hub = DataHub(st, providers)
        fund = hub.get_latest_fundamentals(syms, asof=None)
        if not fund:
            print("  QMT 与兜底源均未返回财务（检查网络/账户权限）。")
        for sym, f in fund.items():
            print(f"\n[{sym}] ann_date={f.ann_date} report_period={f.report_period}")
            for k in ("roe", "eps", "bps", "ocf", "revenue_yoy", "profit_yoy",
                      "gross_margin", "debt_ratio", "revenue", "net_profit"):
                v = getattr(f, k)
                print(f"    {k:<14} {v}")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()

    if args.moneyflow:
        print("\n=== 资金流自检（transactioncount1d）===")
        ms, me = "20260601", "20260810"
        print(f"  发起资金流下载（异步，超时 {args.mf_timeout}s）...", flush=True)

        def mf_worker():
            xt.download_history_data2(syms, "transactioncount1d", ms, me, None)
            deadline = time.time() + args.mf_timeout
            while time.time() < deadline:
                raw = xt.get_market_data_ex(
                    field_list=[], stock_list=syms, period="transactioncount1d",
                    start_time=ms, end_time=me, dividend_type="none", fill_data=False)
                if raw and any(df is not None and len(df) for df in raw.values()):
                    return raw
                time.sleep(0.5)
            return None

        raw = _run_in_thread(mf_worker, args.mf_timeout + 10, "资金流")
        if not raw:
            print("  QMT 资金流下载未就绪/超时（该数据可能需 L2 权限）。")
        else:
            for sym in syms:
                df = (raw or {}).get(sym)
                if df is None or len(df) == 0:
                    print(f"[{sym}] 资金流：空")
                    continue
                print(f"\n[{sym}] transactioncount1d 列: {sorted(df.columns)}")
                print(f"[{sym}] 行数: {len(df)}，最新一行:")
                print(df.tail(1).to_string())
        try:
            mf = p.get_money_flow(syms, ms, me)
            print(f"\n标准化资金流输出: {len(mf)} 行")
            if not mf.empty:
                print(mf.tail(3).to_string(index=False))
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
