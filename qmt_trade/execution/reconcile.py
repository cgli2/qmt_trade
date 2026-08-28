"""盘后对账 Gate-3（设计 6.6.3）。

对账是全自动交易系统里**唯一能发现"我以为的"和"实际的"不一致**的机制。
断线期间丢失的成交回报、部分成交没回来、手工在券商端动了仓——
这些都只会在对账时暴露。所以这里的默认结论是**不通过**：
只有每一项都对得上才算通过，任何查询失败、任何解析异常都判失败。

失败的后果是明确的：``KillSwitch → REDUCE_ONLY``，次日禁止开仓，直到人工确认。
这正是修正 qmt_etf「对账只打日志」的关键点——发现问题必须联动阻断，否则等于没查。

差异分四类，严重度递减：
- ``POSITION_MISSING``  券商有、本地无 —— 最危险，说明有笔成交我们完全不知道；
- ``POSITION_EXTRA``    本地有、券商无 —— 幻觉持仓，会导致卖出失败；
- ``POSITION_QTY``      数量不一致 —— 通常是部分成交回报丢失；
- ``CASH_MISMATCH``     现金差 —— 多为费用口径差异，超容忍度才报。
另有 ``TRADE_UNKNOWN``（券商流水里有本地没记的成交）与 ``SLIPPAGE``（成交价异常偏离）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Protocol

from ..core.logging import get_logger

logger = get_logger("execution.reconcile")

#: 严重度顺序，用于取最高级
_SEV_ORDER = {"INFO": 0, "WARN": 1, "ERROR": 2, "CRITICAL": 3}


@dataclass(frozen=True)
class Discrepancy:
    kind: str
    symbol: str = ""
    local: Any = None
    broker: Any = None
    severity: str = "ERROR"
    message: str = ""

    @property
    def blocking(self) -> bool:
        """是否严重到必须停开仓。"""
        return _SEV_ORDER.get(self.severity, 2) >= _SEV_ORDER["ERROR"]

    def render(self) -> str:
        loc = "-" if self.local is None else self.local
        brk = "-" if self.broker is None else self.broker
        head = f"[{self.severity}] {self.kind}"
        if self.symbol:
            head += f" {self.symbol}"
        return f"{head}: 本地={loc} 券商={brk} {self.message}".rstrip()


@dataclass
class ReconcileResult:
    trade_date: date
    passed: bool = True
    checked: dict[str, int] = field(default_factory=dict)
    discrepancies: list[Discrepancy] = field(default_factory=list)
    error: str = ""

    @property
    def blocking(self) -> list[Discrepancy]:
        return [d for d in self.discrepancies if d.blocking]

    def add(self, d: Discrepancy) -> None:
        self.discrepancies.append(d)
        if d.blocking:
            self.passed = False

    def render(self) -> str:
        lines = ["=" * 60,
                 f"盘后对账 {self.trade_date}  "
                 f"{'通过' if self.passed else '未通过'}",
                 "=" * 60]
        cnt = ", ".join(f"{k}={v}" for k, v in self.checked.items())
        if cnt:
            lines.append(f"  核对范围: {cnt}")
        if self.error:
            lines.append(f"  ✖ 执行异常: {self.error}")
        if not self.discrepancies:
            lines.append("  无差异")
        else:
            for d in self.discrepancies:
                lines.append("  " + d.render())
        if not self.passed:
            lines.append("-" * 60)
            lines.append("  ⚠ 已阻断次日开仓，需人工确认后 `cli reconcile --ack`")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"trade_date": self.trade_date.isoformat(), "passed": self.passed,
                "checked": self.checked, "error": self.error,
                "discrepancies": [{"kind": d.kind, "symbol": d.symbol,
                                   "local": d.local, "broker": d.broker,
                                   "severity": d.severity, "message": d.message}
                                  for d in self.discrepancies]}


class BrokerView(Protocol):
    """券商侧只读视图。QMTGateway 实现它，测试用假对象实现它。"""

    def query_positions(self) -> list[Mapping[str, Any]]: ...
    def query_asset(self) -> Mapping[str, Any]: ...
    def query_trades(self, trade_date: date) -> list[Mapping[str, Any]]: ...


class Reconciler:
    """本地账本 vs 券商真实状态。

    ``local_positions`` 形如 ``{symbol: shares}``；``local_cash`` 是可用现金。
    既可以从 ``repos``（数据库视图）取，也可以直接从内存 ``PortfolioState`` 取——
    实盘用前者（重启后仍可对账），回测/单测用后者。
    """

    def __init__(self, settings=None, *, repos=None, killswitch=None, notifier=None):
        cfg = (settings.section("risk").get("gate3", {}) if settings is not None else {})
        cfg = cfg if isinstance(cfg, dict) else {}
        self.cash_tolerance = float(cfg.get("cash_tolerance", 1.0))
        self.position_tolerance = int(cfg.get("position_tolerance", 0))
        self.slippage_alert = float(cfg.get("slippage_alert", 0.01))
        self.repos = repos
        self.killswitch = killswitch
        self.notifier = notifier

    # ------------------------------------------------------------ 取本地视图
    def _local_positions(self, override: Mapping[str, int] | None) -> dict[str, int]:
        if override is not None:
            return {k: int(v) for k, v in override.items() if int(v) != 0}
        if self.repos is None:
            return {}
        return {r["symbol"]: int(r["volume"] or 0)
                for r in self.repos.positions.list_all() if int(r["volume"] or 0) != 0}

    def _local_cash(self, override: float | None) -> float | None:
        if override is not None:
            return float(override)
        if self.repos is None:
            return None
        row = self.repos.snapshots.latest()
        return float(row["cash"]) if row else None

    def _local_trades(self, d: date) -> list[dict]:
        if self.repos is None:
            return []
        return self.repos.trades.list_by_date(d)

    # ------------------------------------------------------------ 主流程
    def run(self, trade_date: date, broker: BrokerView, *,
            local_positions: Mapping[str, int] | None = None,
            local_cash: float | None = None,
            persist: bool = True, notify: bool = True) -> ReconcileResult:
        res = ReconcileResult(trade_date=trade_date)
        try:
            self._compare_positions(res, broker, local_positions)
            self._compare_cash(res, broker, local_cash)
            self._compare_trades(res, broker, trade_date)
        except Exception as exc:                    # 查询/解析失败一律判不通过
            res.passed = False
            res.error = f"{type(exc).__name__}: {exc}"
            logger.exception("对账过程异常")

        if persist:
            self._persist(res)
        self._react(res, notify=notify)
        return res

    # ------------------------------------------------------------ 各项比对
    def _compare_positions(self, res: ReconcileResult, broker: BrokerView,
                           override: Mapping[str, int] | None) -> None:
        local = self._local_positions(override)
        raw = broker.query_positions() or []
        remote: dict[str, int] = {}
        for r in raw:
            sym = str(r.get("symbol") or r.get("stock_code") or "")
            qty = int(r.get("volume") or r.get("m_nVolume") or 0)
            if sym and qty != 0:
                remote[sym] = remote.get(sym, 0) + qty
        res.checked["positions"] = len(set(local) | set(remote))

        for sym in sorted(set(local) | set(remote)):
            lv, rv = local.get(sym, 0), remote.get(sym, 0)
            if lv == rv:
                continue
            if lv == 0:
                res.add(Discrepancy("POSITION_MISSING", sym, 0, rv, "CRITICAL",
                                    "券商有持仓但本地无记录，可能有未知成交"))
            elif rv == 0:
                res.add(Discrepancy("POSITION_EXTRA", sym, lv, 0, "CRITICAL",
                                    "本地有持仓但券商无，幻觉持仓会导致卖出失败"))
            elif abs(lv - rv) > self.position_tolerance:
                res.add(Discrepancy("POSITION_QTY", sym, lv, rv, "ERROR",
                                    f"数量差 {lv - rv:+d} 股"))

    def _compare_cash(self, res: ReconcileResult, broker: BrokerView,
                      override: float | None) -> None:
        local = self._local_cash(override)
        if local is None:
            res.add(Discrepancy("CASH_UNKNOWN", "", None, None, "WARN",
                                "本地无现金快照，跳过现金对账"))
            return
        asset = broker.query_asset() or {}
        remote = asset.get("cash", asset.get("m_dCash"))
        if remote is None:
            res.add(Discrepancy("CASH_UNKNOWN", "", local, None, "ERROR",
                                "券商未返回现金字段"))
            return
        remote = float(remote)
        res.checked["cash"] = 1
        diff = local - remote
        if abs(diff) > self.cash_tolerance:
            res.add(Discrepancy("CASH_MISMATCH", "", round(local, 2), round(remote, 2),
                                "ERROR", f"差额 {diff:+,.2f} 元"))

    def _compare_trades(self, res: ReconcileResult, broker: BrokerView,
                        d: date) -> None:
        remote = list(broker.query_trades(d) or [])
        local = self._local_trades(d)
        res.checked["trades"] = max(len(remote), len(local))
        # 刻意不在 remote 为空时提前返回：券商说今天没成交、本地却记了单，
        # 恰恰是最需要报警的情形（回报丢失或本地误记），下面会走成 TRADE_PHANTOM。

        # 按 (symbol, side, volume) 做多重集匹配：券商侧成交编号与本地 ID 无法直接对齐
        def _key(sym: str, side: str, vol: int) -> tuple[str, str, int]:
            s = str(side).upper()
            s = "BUY" if s in ("BUY", "48", "23", "买入") else \
                "SELL" if s in ("SELL", "49", "24", "卖出") else s
            return (sym, s, int(vol))

        # 存整行而不是计数：滑点要跟**配对上的那一笔**比，同标的多笔不同价才不会比错
        pool: dict[tuple, list[Mapping[str, Any]]] = {}
        for t in local:
            k = _key(str(t["symbol"]), str(t["side"]), int(t["volume"]))
            pool.setdefault(k, []).append(t)

        for t in remote:
            sym = str(t.get("symbol") or t.get("stock_code") or "")
            vol = int(t.get("volume") or t.get("m_nTradedVolume") or 0)
            price = float(t.get("price") or t.get("m_dTradedPrice") or 0.0)
            k = _key(sym, t.get("side") or t.get("m_nOffsetFlag") or "", vol)
            rows = pool.get(k)
            if rows:
                self._check_slippage(res, sym, price, rows.pop(0))
            else:
                res.add(Discrepancy("TRADE_UNKNOWN", sym, None,
                                    f"{k[1]} {vol}@{price:.2f}", "CRITICAL",
                                    "券商流水中存在本地未记录的成交"))

        for k, rows in pool.items():
            if rows:
                res.add(Discrepancy("TRADE_PHANTOM", k[0],
                                    f"{k[1]} {k[2]}×{len(rows)}", None,
                                    "ERROR", "本地记录的成交在券商流水中不存在"))

    def _check_slippage(self, res: ReconcileResult, sym: str, broker_price: float,
                        row: Mapping[str, Any]) -> None:
        if broker_price <= 0 or not row:
            return
        lp = float(row.get("price") or 0)
        if lp <= 0:
            return
        dev = abs(broker_price - lp) / lp
        if dev > self.slippage_alert:
            res.add(Discrepancy("SLIPPAGE", sym, round(lp, 3), round(broker_price, 3),
                                "WARN", f"成交价偏离 {dev:.2%}"))

    # ------------------------------------------------------------ 落库与联动
    def _persist(self, res: ReconcileResult) -> None:
        if self.repos is None:
            return
        try:
            self.repos.db.insert("reconcile_logs", {
                "id": f"rec_{res.trade_date.isoformat()}_{int(time.time())}",
                "trade_date": res.trade_date.isoformat(),
                "passed": 1 if res.passed else 0,
                "detail": json.dumps(res.to_dict(), ensure_ascii=False, default=str),
                "created_at": time.time()})
            self.repos.system.set("reconcile_ok", "1" if res.passed else "0",
                                  reason=res.render()[:500])
            for d in res.discrepancies:
                self.repos.risk_events.add(
                    "GATE3", d.kind, d.message, trade_date=res.trade_date,
                    symbol=d.symbol or None, severity=d.severity,
                    detail=json.dumps({"local": d.local, "broker": d.broker},
                                      ensure_ascii=False, default=str))
        except Exception as exc:                     # 落库失败不改变对账结论
            logger.warning("对账结果落库失败: %s", exc)

    def _react(self, res: ReconcileResult, *, notify: bool) -> None:
        if not res.passed:
            if self.killswitch is not None:
                self.killswitch.engage(
                    f"{res.trade_date} 对账未通过（{len(res.blocking)} 项差异），禁止开仓")
            logger.error("对账未通过:\n%s", res.render())
        else:
            logger.info("对账通过 %s（%s）", res.trade_date,
                        ", ".join(f"{k}={v}" for k, v in res.checked.items()) or "空账户")
        if notify and self.notifier is not None:
            # 通知是旁路：即便通道全挂，对账结论与阻断动作也必须已经生效（P4）
            try:
                if res.passed:
                    self.notifier.notify(f"对账通过 {res.trade_date}", level="DEBUG",
                                         key=f"reconcile:{res.trade_date}")
                else:
                    body = "\n".join(d.render() for d in res.discrepancies[:10])
                    self.notifier.notify(
                        f"⚠ 对账未通过 {res.trade_date}", body or res.error,
                        level="CRITICAL", key=f"reconcile:{res.trade_date}")
            except Exception as exc:
                logger.warning("对账结果通知失败: %s", exc)

    # ------------------------------------------------------------ 人工确认
    def acknowledge(self, trade_date: date, *, operator: str = "manual",
                    note: str = "") -> bool:
        """人工确认差异已处理，解除次日开仓限制。

        刻意要求显式调用而非自动恢复：对账失败意味着账实不符，
        必须有人真的去看过券商端才能继续交易。
        """
        if self.repos is not None:
            try:
                self.repos.system.set("reconcile_ok", "1",
                                      reason=f"{operator} 确认 {trade_date}: {note}")
                self.repos.db.insert("reconcile_logs", {
                    "id": f"ack_{trade_date.isoformat()}_{int(time.time())}",
                    "trade_date": trade_date.isoformat(), "passed": 1,
                    "detail": json.dumps({"ack": True, "operator": operator,
                                          "note": note}, ensure_ascii=False),
                    "created_at": time.time()})
            except Exception as exc:
                logger.warning("确认落库失败: %s", exc)
                return False
        if self.killswitch is not None:
            self.killswitch.reset(f"{operator} 确认对账差异：{note or '已核实'}")
        logger.warning("对账差异已由 %s 人工确认：%s", operator, note or "-")
        return True
