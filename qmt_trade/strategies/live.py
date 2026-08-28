"""策略实验室实时运行器（v1，2026-08-16）。

把独立策略（打板/二板/尾盘低吸/趋势买点）接入调度器与主策略池：

* **运行/停止**：由 ``strategies.<sid>.enabled`` 控制 —— WebUI「策略实验室」页的
  启用开关即运行/停止开关（调度任务读取实时配置，勾选后下一次触发即生效，无需重启）。
* **每日周期**（日线级管理，paper-first）：
  - ``open`` 相位（09:35）：打板/二板开盘买入（T-1 选池 + T 开盘涨幅过滤）；
  - ``close`` 相位（14:45）：尾盘低吸/趋势买点尾盘买入 + 全部策略的持仓管理
    （止损/止盈/时间止损，基于当日 bar，与回测器同口径）+ 记录当日收益到策略池。
* **持仓元数据**（止损价/止盈档/入场日）存 ``system_state: strategylab:meta:<sid>:<sym>``。
* **主策略池**：趋势买点注册进 ``ctx.pool``（影子状态），记录日收益，
  仓位 = position_fraction × 池权重（SHADOW/未注册权重 0 时不下单仅记账 —— 与池的
  "新策略先影子"纪律一致；种子权重可配 ``evolution.pool.seed_weights``）。

局限（v1）：日线级管理 —— 盘中止损按当日 low 触发后以触发价提交（非盘中实时监控）；
打板"破板次日开盘卖"在 v1 中于 14:45 提交卖出。实盘前需按此清单升级分钟级。
"""
from __future__ import annotations

import json
import logging
from datetime import date

from ..datahub.types import Adjust, Freq

logger = logging.getLogger("strategies.live")

#: 每个策略的入场相位（open=09:35 开盘买 / close=14:45 尾盘买）
ENTRY_PHASE = {"limit_up": "open", "second_board": "open",
               "dip_buy": "close", "trend_buy": "close"}
LAB_SIDS = tuple(ENTRY_PHASE)


class LabLiveRunner:
    """单策略实时每日周期。jr = scheduler.jobs.JobRunner（含 ctx 与交易日历/撮合助手）。"""

    def __init__(self, jr, sid: str):
        self.jr = jr
        self.ctx = jr.ctx
        self.sid = sid

    # ============================================================ 配置/元数据
    def _cfg(self):
        from ..strategies.base import load_config
        from . import dip_buy, limit_up, second_board, trend_buy  # noqa: F401
        cls = {"limit_up": limit_up.LimitUpConfig, "second_board": second_board.SecondBoardConfig,
               "dip_buy": dip_buy.DipBuyConfig, "trend_buy": trend_buy.TrendBuyConfig}[self.sid]
        return load_config(self.ctx.settings, cls, self.sid)

    def _meta_key(self, sym: str) -> str:
        return f"strategylab:meta:{self.sid}:{sym}"

    def _meta_get(self, sym: str) -> dict | None:
        raw = self.ctx.shared_repos.system.get(self._meta_key(sym))
        try:
            return json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            return None

    def _meta_set(self, sym: str, meta: dict) -> None:
        self.ctx.shared_repos.system.set(
            self._meta_key(sym), json.dumps(meta, ensure_ascii=False, default=str),
            reason=f"strategylab {self.sid} 持仓元数据")

    def _meta_del(self, sym: str) -> None:
        try:
            self.ctx.shared_repos.system.delete(self._meta_key(sym))
        except Exception:  # noqa: BLE001
            pass

    # ============================================================ 持仓定位
    def _strategy_positions(self) -> list[dict]:
        """本策略的持仓（plan_id 前缀 slb_<sid>_ 或存在元数据）。"""
        out = []
        prefix = f"slb_{self.sid}_"
        try:
            for p in self.ctx.repos.positions.list_all():
                sym = str(p.get("symbol") or "")
                if sym and (str(p.get("plan_id") or "").startswith(prefix)
                            or self._meta_get(sym)):
                    out.append(p)
        except Exception as exc:  # noqa: BLE001
            logger.warning("strategylab 读取持仓失败 %s: %s", self.sid, exc)
        return out

    # ============================================================ 每日周期
    def daily(self, phase: str) -> dict:
        cfg = self._cfg()
        if not cfg.enabled:
            return {"sid": self.sid, "skipped": True, "reason": "enabled=false（UI 停用）"}
        if not self.ctx.calendar.is_trading_day(self.jr.today):
            return {"sid": self.sid, "skipped": True, "reason": "非交易日"}
        res: dict = {"sid": self.sid, "managed": 0, "opened": 0, "skipped": False}
        self._manage(cfg, res)
        if ENTRY_PHASE.get(self.sid) == phase:
            self._enter(cfg, res)
        if phase == "close" and self.sid == "trend_buy":
            self._record_pool_return()
        return res

    # ============================================================ 管理
    def _manage(self, cfg, res: dict) -> None:
        for pos in self._strategy_positions():
            sym = str(pos.get("symbol") or "")
            if not sym:
                continue
            meta = self._meta_get(sym) or {}
            bar = self._bar(sym)
            if bar is None:
                continue
            hi, lo, op = bar.get("high", 0), bar.get("low", 0), bar.get("open", 0)
            close = bar.get("close", 0)
            stop = float(meta.get("stop_price") or 0)
            entry = float(meta.get("entry_ref") or 0)
            # 动态止损（除权/成本调整自适应）：用当前成本×stop_pct 重算，
            # 成本未变时与绝对止损等价；除权后随成本下移，避免假触发。
            stop_pct = float(meta.get("stop_pct") or 0)
            if stop_pct > 0:
                cur = self._pos_avg_cost(sym) or entry
                stop_dyn = cur * (1 - stop_pct)
                if stop_dyn > 0:
                    stop = stop_dyn
            reason = ref = None
            if stop > 0 and lo and lo <= stop:
                reason = f"{self.sid}_STOP"
                ref = op if (op and op <= stop) else stop
            elif self.sid in ("dip_buy", "trend_buy") and hi and entry > 0:
                tp2 = entry * (1 + float(meta.get("tp2", 0.35)))
                if hi >= tp2:
                    reason, ref = f"{self.sid}_TP2", tp2
                elif not meta.get("tp1_done"):
                    tp1 = entry * (1 + float(meta.get("tp1", 0.20)))
                    if hi >= tp1:
                        reason, ref = f"{self.sid}_TP1", tp1
                        meta["tp1_done"] = True
                        self._meta_set(sym, meta)
            elif self.sid in ("limit_up", "second_board") and close:
                lu = bar.get("limit_up") or 0
                if not (lu > 0 and close >= lu * 0.999):
                    reason, ref = f"{self.sid}_BREAK", close
            if reason is None and meta.get("opened_at"):
                try:
                    opened = date.fromisoformat(str(meta["opened_at"]))
                    if (self.jr.today - opened).days >= int(meta.get("max_hold_days", 20)):
                        reason, ref = f"{self.sid}_TIME_EXIT", close
                except (TypeError, ValueError):
                    pass
            if reason and ref and self._sell(sym, ref, reason):
                res["managed"] += 1
                self._meta_del(sym)

    # ============================================================ 入场
    def _enter(self, cfg, res: dict) -> None:
        from ..core.strategies import build_standalone_backtester
        bt = build_standalone_backtester(self.sid, self.ctx.settings, self.ctx.hub)
        bt.universe = self._universe()
        try:
            bt._prewarm(self.jr.today, self.jr.today)
        except Exception as exc:  # noqa: BLE001
            logger.warning("strategylab %s 面板预热失败: %s", self.sid, exc)
            return
        instr_map = bt._instrument_map(bt.universe)
        if not bt._market_ok(self.jr.today):
            return  # 弱市空仓
        try:
            picks = bt._screen(self.jr.today, instr_map)
        except Exception as exc:  # noqa: BLE001
            logger.warning("strategylab %s 选股失败: %s", self.sid, exc)
            return
        weight = self._pool_weight()
        for sym in picks:
            bar = self._bar(sym)
            if bar is None:
                continue
            if self.sid in ("limit_up", "second_board"):
                ref = bar.get("open") or 0
                prev = bar.get("prev_close") or 0
                if prev <= 0 or not (cfg.open_gap_lo <= ref / prev - 1 <= cfg.open_gap_hi):
                    continue
            else:
                ref = bar.get("close") or 0
            if ref <= 0:
                continue
            meta = self._buy_meta(cfg, ref)
            if self._buy(sym, ref, f"{self.sid}_BUY", meta, weight):
                res["opened"] += 1

    def _buy_meta(self, cfg, ref: float) -> dict:
        meta = {"opened_at": self.jr.today.isoformat(), "entry_ref": round(ref, 4),
                "max_hold_days": cfg.max_hold_days}
        if self.sid in ("dip_buy", "trend_buy"):
            meta["tp1"] = cfg.take_profit1
            meta["tp2"] = cfg.take_profit2
            meta["stop_pct"] = round(cfg.stop_floor_pct, 4)
            meta["stop_price"] = round(ref * (1 - cfg.stop_floor_pct), 4)
        elif self.sid in ("limit_up", "second_board"):
            meta["stop_pct"] = round(cfg.stop_pct, 4)
            meta["stop_price"] = round(ref * (1 - cfg.stop_pct), 4)
        return meta

    def _pos_avg_cost(self, sym: str) -> float | None:
        try:
            pos = self.ctx.portfolio.positions.get(sym)
            return float(pos.avg_cost) if pos and pos.avg_cost else None
        except Exception:  # noqa: BLE001
            return None

    # ============================================================ 策略池
    def _pool_weight(self) -> float:
        """主策略池权重（SHADOW/未注册 → 0，不实际下单）。"""
        try:
            rec = self.ctx.pool.strategies.get(self.sid)
            return float(rec.weight or 0.0) if rec else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _lab_equity(self) -> float:
        """lab 切片权益 = 初始分配 + 累计已实现盈亏 + 当前持仓市值。

        修复（2026-08-16）：此前用整仓 total_asset 记录，把主因子策略的盈亏
        也记进了 trend_buy —— 池子调权会失真。这里只统计本策略自己的
        （order_id/plan_id 前缀 slb_<sid>_ 的成交与持仓）。
        """
        prefix = f"slb_{self.sid}_"
        realized = 0.0
        try:
            for t in self.ctx.repos.trades.list_all():
                oid = str(t.get("order_id") or "")
                pnl = t.get("realized_pnl")
                if oid.startswith(prefix) and pnl is not None:
                    realized += float(pnl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lab 已实现盈亏读取失败: %s", exc)
        value = 0.0
        try:
            for p in self.ctx.repos.positions.list_all():
                if str(p.get("plan_id") or "").startswith(prefix):
                    shares = float(p.get("shares") or 0)
                    px = float(p.get("last_price") or 0) or float(p.get("avg_cost") or 0)
                    value += shares * px
        except Exception as exc:  # noqa: BLE001
            logger.warning("lab 持仓市值读取失败: %s", exc)
        base_key = f"strategylab:cap:{self.sid}:base"
        raw = self.ctx.shared_repos.system.get(base_key)
        if not raw:
            base = self._pool_weight() * self.ctx.portfolio.total_asset
            try:
                self.ctx.shared_repos.system.set(base_key, f"{base:.4f}",
                                                 reason=f"lab {self.sid} 初始分配")
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                base = float(raw)
            except (TypeError, ValueError):
                base = 0.0
        return base + realized + value

    def _record_pool_return(self) -> None:
        try:
            pool = self.ctx.pool
            pool.register(self.sid)   # 影子入池（幂等）
            eq = self._lab_equity()
            prev_key = f"strategylab:eq:{self.sid}:prev"
            raw = self.ctx.shared_repos.system.get(prev_key)
            try:
                last_eq = float(raw) if raw else 0.0
            except (TypeError, ValueError):
                last_eq = 0.0
            if last_eq > 0 and eq > 0:
                pool.record(self.sid, eq / last_eq - 1.0, self.jr.today)
            self.ctx.shared_repos.system.set(prev_key, f"{eq:.4f}",
                                             reason=f"strategylab {self.sid} 权益基准")
            self.ctx.save_pool()
        except Exception as exc:  # noqa: BLE001
            logger.warning("strategylab 策略池记录失败: %s", exc)

    # ============================================================ 工具
    def _universe(self) -> list[str]:
        try:
            infos = self.ctx.hub.get_instruments()
            return list(infos.keys()) if isinstance(infos, dict) else \
                [getattr(i, "symbol", str(i)) for i in (infos or [])]
        except Exception:  # noqa: BLE001
            return []

    def _bar(self, sym: str) -> dict | None:
        try:
            df = self.ctx.hub.get_bars([sym], Freq.D1, self.jr.today, self.jr.today,
                                       Adjust.NONE, validate=True)
        except Exception:  # noqa: BLE001
            return None
        if df is None or df.empty:
            return None
        row = df.iloc[-1]
        return {c: row.get(c) for c in ("open", "high", "low", "close",
                                        "limit_up", "prev_close")}

    def _buy(self, sym, ref, signal, meta, weight) -> bool:
        from ..brain.schemas import TradeIntent
        if weight <= 0:
            # 影子/权重 0：不下单，仅记录元数据（纸面运行，等池子给权重）
            self._meta_set(sym, meta)
            logger.info("strategylab %s %s 池权重 0，影子运行（不下单）", self.sid, sym)
            return True
        # 止损接入执行链：STRUCTURE + 绝对价（>1 按绝对价，见 service._stop_price）。
        # 修复（2026-08-16）：此前 stop_loss_value=0 会被兑成默认 -7% 止损，
        # 与策略自身的破位止损不一致；现直接挂策略止损价，Gate-2 盘中即可按
        # 正确价位触发（外部止损由 intraday 任务以 stop_pct×成本 实时重算）。
        stop_price = float(meta.get("stop_price") or 0)
        stop_type = "STRUCTURE" if stop_price > 1 else "FIXED_PCT"
        stop_value = stop_price if stop_price > 1 else float(meta.get("stop_pct") or 0)
        it = TradeIntent(
            symbol=sym, action="BUY", confidence=0.9, conviction="MEDIUM",
            entry_type="LIMIT", entry_ref_price=round(ref, 4),
            stop_loss_type=stop_type, stop_loss_value=stop_value,
            max_weight_hint=weight * self._cfg().position_fraction,
            max_holding_days=int(meta.get("max_hold_days", 20)),
            valid_until=self.jr.today,
            reasoning=f"策略实验室[{self.sid}]买入（池权重 {weight:.0%}）")
        res = self.jr._submit_intent(it, bar=self._to_bar(sym, ref),
                                     plan_id=f"slb_{self.sid}_{self.jr.today:%Y%m%d}",
                                     signal=signal)
        if res.ok:
            self._meta_set(sym, meta)
            return True
        logger.warning("strategylab %s 买入被拒 %s: %s", self.sid, sym, getattr(res, "reason", "?"))
        return False

    def _sell(self, sym, ref, signal) -> bool:
        from ..brain.schemas import TradeIntent
        it = TradeIntent(
            symbol=sym, action="SELL", confidence=1.0, conviction="HIGH",
            entry_type="MARKET", entry_ref_price=None,
            stop_loss_type="FIXED_PCT", stop_loss_value=0.0,
            max_weight_hint=0.3, max_holding_days=20, valid_until=self.jr.today,
            reasoning=f"策略实验室[{self.sid}]离场：{signal}")
        res = self.jr._submit_intent(it, bar=self._to_bar(sym, ref),
                                     plan_id=f"slb_{self.sid}_{self.jr.today:%Y%m%d}",
                                     signal=signal)
        return bool(res.ok)

    def _to_bar(self, sym, ref):
        from ..datahub.types import Bar
        b = self._bar(sym) or {}
        return Bar(symbol=sym, date=self.jr.today, open=float(b.get("open") or ref),
                   high=float(b.get("high") or ref), low=float(b.get("low") or ref),
                   close=float(b.get("close") or ref),
                   volume=float(b.get("volume") or 0), amount=0.0,
                   limit_up=b.get("limit_up"))


__all__ = ["LabLiveRunner", "ENTRY_PHASE", "LAB_SIDS"]
