"""读取回测存档，输出报告用归因统计（离场原因分布 / 盈亏结构 / 月度信号分布）。"""
from __future__ import annotations

import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def show(payload, tag=""):
    for r in payload["rows"]:
        if r.get("error"):
            print(f"[{tag}] {r['name']} ERROR {r['error']}")
            continue
        b = r.get("bench") or {}
        print(f"\n===== [{tag}] {r['name']}  {r['start']} ~ {r['end']} =====")
        print(f"  收益 {r['total_return']:+.2%} | 基准沪深300 {b.get('bench_return', 0):+.2%} "
              f"| 超额 {r['total_return'] - (b.get('bench_return') or 0):+.2%}")
        print(f"  夏普 {r['sharpe']} | 最大回撤 {r['max_dd']:.2%} | 基准回撤 {b.get('bench_max_dd', 0):.2%}")
        print(f"  毛 {r['gross']:+.2%} - 成本 {r['cost']:.2%} = 净 {r['net']:+.2%} "
              f"| 单边换手 {r['turnover']:.1f}x")
        print(f"  平仓 {r['n_closed']} 笔 | 胜率 {r['win_rate']:.1%} | 盈亏比 {r['payoff']:.2f} "
              f"| 均盈 {r['avg_win']:.0f} 均亏 {r['avg_loss']:.0f} | 均持 {r['avg_hold']:.1f}d "
              f"| 信号行 {r['n_signal_rows']}")
        ct = r.get("closed_trades") or []
        if not ct:
            continue
        by_reason = defaultdict(list)
        for t in ct:
            by_reason[t.get("reason", "?")].append(float(t.get("pnl") or 0))
        print("  离场原因分布：")
        for reason, pnls in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            wins = sum(1 for p in pnls if p > 0)
            print(f"    {reason:<14} {len(pnls):>3} 笔 | 合计 {sum(pnls):>+12,.0f} | "
                  f"单笔均值 {sum(pnls)/len(pnls):>+9,.0f} | 胜 {wins}/{len(pnls)}")
        # 持仓天数分布
        holds = [int(t.get("holding_days") or 0) for t in ct]
        buckets = Counter("0-5" if h <= 5 else "6-10" if h <= 10 else "11-20" if h <= 20
                          else "21-30" if h <= 30 else ">30" for h in holds)
        print("  持仓天数分布：", dict(buckets))
        # 极端交易
        top = sorted(ct, key=lambda t: -float(t.get("pnl") or 0))[:3]
        bot = sorted(ct, key=lambda t: float(t.get("pnl") or 0))[:3]
        print("  最佳 3 笔：", [(t["symbol"], t.get("opened_at"), round(float(t["pnl"]))) for t in top])
        print("  最差 3 笔：", [(t["symbol"], t.get("opened_at"), round(float(t["pnl"]))) for t in bot])
        # 月度信号/买入分布
        sl = r.get("signal_log") or []
        if sl:
            mon = Counter(s["date"][:7] for s in sl)
            print("  买入月份分布：", dict(sorted(mon.items())))


def main():
    pat = sys.argv[1] if len(sys.argv) > 1 else ".verify_tmp/trend_breakout_*_1y.json"
    files = sorted(glob.glob(str(ROOT / pat)))
    if not files:
        print("未找到存档：", pat)
        return 1
    for f in files:
        print(f"\n########## {Path(f).name} ##########")
        show(load(f), Path(f).stem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
