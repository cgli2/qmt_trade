"""Phase-3 最小验证：live 账本首建时的现金基线来源。

复现用户报告的 CASH_MISMATCH（本地=1000000.0 券商=53608.39）：
live 模式下账本为空（无快照、无持仓）时，``TradingContext.portfolio``
用哪个数当起始现金？券商真实现金有没有被采纳？

只读取证，不写任何库。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QMT_ALLOW_LIVE", "1")
os.environ.setdefault("QMT_ACCOUNT_ID", "1500125950")
# 后端常驻进程独占 data/db/qmt.duckdb；Settings.load 会经 read_active 去开它。
# 指向一个不存在的临时文件即可让 read_active 直接返回 None（纯读 YAML）。
_TMPDIR = Path(tempfile.mkdtemp(prefix="qmt_probe_"))
os.environ["QMT_RUNTIME_DB"] = str(_TMPDIR / "probe.duckdb")

from qmt_trade.app import TradingContext                  # noqa: E402
from qmt_trade.storage.db import Database                 # noqa: E402
from qmt_trade.storage.models import Repos                # noqa: E402

# 券商真实值（来自 2026-09-15 /api/trade/positions?mode=live 与对账输出）
BROKER_CASH = 53_608.39
BROKER_POSITIONS = [
    {"symbol": "600216.SH", "volume": 1000, "can_use": 1000, "avg_cost": 14.1059},
    {"symbol": "300570.SZ", "volume": 600, "can_use": 600, "avg_cost": 234.1224},
]


class FakeBroker:
    """券商只读视图替身，口径与 QMTGateway.query_* 一致。"""

    def __init__(self):
        self.calls: list[str] = []

    def query_positions(self):
        self.calls.append("positions")
        return list(BROKER_POSITIONS)

    def query_asset(self):
        self.calls.append("asset")
        return {"cash": BROKER_CASH}

    def query_trades(self, trade_date):
        self.calls.append("trades")
        return []


def main() -> int:
    broker = FakeBroker()
    repos = Repos.create(Database(":memory:"))
    ctx = TradingContext(mode="live", repos=repos, gateway=broker)

    print("=" * 62)
    print("Phase-3 最小验证：live 空账本的现金基线")
    print("=" * 62)
    print(f"  mode                          = {ctx.mode}  is_live={ctx.is_live}")
    print(f"  snapshots.latest()            = {repos.snapshots.latest()}")
    print(f"  positions.list_all()          = {repos.positions.list_all()}")
    print(f"  settings backtest.initial_cash"
          f" = {ctx.settings.get('backtest.initial_cash')}")
    print(f"  券商 query_asset().cash       = {BROKER_CASH:,.2f}")

    ps = ctx.portfolio
    print("-" * 62)
    print(f"  ctx.portfolio.cash            = {ps.cash:,.2f}")
    print(f"  ctx.portfolio.initial_asset   = {ps.initial_asset:,.2f}")
    print(f"  ctx.portfolio.positions       = {sorted(ps.positions)}")
    print(f"  broker 被调用过的方法          = {broker.calls or '（无）'}")
    print("-" * 62)

    adopted_cash = abs(ps.cash - BROKER_CASH) < 0.01
    adopted_pos = set(ps.positions) == {p["symbol"] for p in BROKER_POSITIONS}
    print(f"  [采纳券商现金]   {adopted_cash}")
    print(f"  [采纳券商持仓]   {adopted_pos}")
    if not adopted_cash:
        src = ctx.settings.get("backtest.initial_cash")
        print(f"  ==> 复现：live 起始现金 = {ps.cash:,.2f}"
              f"（来自 backtest.initial_cash={src}），与券商真实"
              f" {BROKER_CASH:,.2f} 差 {ps.cash - BROKER_CASH:+,.2f} 元")
    ctx.close()
    return 0 if (adopted_cash and adopted_pos) else 1


if __name__ == "__main__":
    raise SystemExit(main())
