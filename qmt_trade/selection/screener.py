"""L0 硬过滤（设计 6.3 第一级漏斗）。

目标：把全市场 ~5400 只压到 1500–2500 只，成本接近零，全部是确定性规则。

三条设计约束：

1. **全向量化**。全市场逐只 Python 循环在实盘盘前时间窗里是不可接受的。
2. **每条规则的淘汰数必须留痕**。这是本模块最重要的产出 ——
   实盘某天候选池突然从 2000 掉到 50，运维要能在 3 秒内看出是哪个闸门卡住了，
   而不是去翻日志猜。所以 ``ScreenResult.stats`` 是一等公民，不是调试附属品。
3. **严格 PIT**。盘前 09:00 做决策时，当日的 OHLC 尚不存在。
   所以"一字板"判断只能基于 **T-1** 数据（昨日一字涨停 → 今日大概率高开买不进），
   真正的实时一字板拦截由 ``execution/guard`` 在下单前再做一次。
   这里做的是"降低无效候选"，不是"保证一定能成交"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from ..core.instruments import Board, detect_board, normalize_symbol
from ..core.logging import get_logger

logger = get_logger("selection.screener")

#: 规则执行顺序。刻意把"零成本且淘汰量大"的规则排前面，减少后续规则的计算量；
#: 同时这个顺序决定了 ``rejected`` 里记录的是**首个**命中的原因（更接近根本原因）。
RULE_ORDER = (
    "blacklist",
    "board",
    "st",
    "list_days",
    "suspended",
    "limit_locked",
    "amount",
    "market_cap",
    "price",
)


@dataclass
class FunnelStage:
    """漏斗某一级的统计。"""

    rule: str
    desc: str
    before: int
    after: int

    @property
    def removed(self) -> int:
        return self.before - self.after

    @property
    def removed_pct(self) -> float:
        return self.removed / self.before if self.before else 0.0

    def __str__(self) -> str:
        return (
            f"{self.rule:<14} {self.desc:<26} "
            f"{self.before:>5} → {self.after:>5}  (-{self.removed}, {self.removed_pct:.1%})"
        )


@dataclass
class ScreenResult:
    asof: date
    passed: list[str]
    #: symbol → 首个命中的淘汰原因（人类可读）
    rejected: dict[str, str] = field(default_factory=dict)
    stats: list[FunnelStage] = field(default_factory=list)
    #: 每只票 × 每条规则的布尔通过表，排查个案用
    detail: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def n_in(self) -> int:
        return len(self.passed) + len(self.rejected)

    @property
    def n_out(self) -> int:
        return len(self.passed)

    def why(self, symbol: str) -> str:
        """单只票为什么被刷掉。实盘答疑最常用的一个方法。"""
        sym = normalize_symbol(symbol)
        if sym in self.rejected:
            return self.rejected[sym]
        return "通过" if sym in self.passed else "不在候选池内"

    def funnel_report(self) -> str:
        head = f"L0 硬过滤漏斗 asof={self.asof}  {self.n_in} → {self.n_out}"
        lines = [head, "-" * len(head)]
        lines += [f"  {s}" for s in self.stats]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "asof": str(self.asof),
            "n_in": self.n_in,
            "n_out": self.n_out,
            "passed": list(self.passed),
            "stats": [
                {"rule": s.rule, "desc": s.desc, "before": s.before,
                 "after": s.after, "removed": s.removed}
                for s in self.stats
            ],
        }


class Screener:
    """L0 硬过滤器。

    用法::

        scr = Screener(settings)
        res = scr.screen(panel, asof=date(2026, 8, 7), instruments=info_map)
        print(res.funnel_report())
    """

    def __init__(self, settings):
        cfg = settings.section("selection.screener") or {}
        self.exclude_st = bool(cfg.get("exclude_st", True))
        self.min_list_days = int(cfg.get("min_list_days", 60))
        self.exclude_suspended = bool(cfg.get("exclude_suspended", True))
        self.exclude_limit_locked = bool(cfg.get("exclude_limit_locked", True))
        self.min_amount_20d = float(cfg.get("min_amount_20d", 5e7))
        self.min_market_cap = float(cfg.get("min_market_cap", 2e9))
        self.allowed_boards = {
            str(b).upper() for b in (cfg.get("allowed_boards") or ["MAIN", "GEM", "STAR"])
        }
        self.blacklist = {normalize_symbol(s) for s in (cfg.get("blacklist") or [])}
        self.min_price = float(cfg.get("min_price", 1.0))
        self.max_price = float(cfg.get("max_price", 0) or 0) or float("inf")
        #: 一字板判定容差：|high-low|/prev_close 小于此值且涨跌幅接近限制即视为一字
        self.limit_lock_tol = float(cfg.get("limit_lock_tol", 0.005))
        self.amount_window = int(cfg.get("amount_window", 20))

    # ------------------------------------------------------------------ 主流程
    def screen(
        self,
        panel: pd.DataFrame,
        *,
        asof: date,
        instruments: dict | None = None,
    ) -> ScreenResult:
        """在 panel（盘前切片后的长表）上执行硬过滤。

        :param panel: 至少包含 date/symbol/open/high/low/close/amount 列的长表。
                      **必须已做盘前 PIT 切片**（最后一根 bar 是 T-1）。
        :param instruments: symbol → InstrumentInfo。缺失时相关规则自动跳过并告警。
        """
        if panel is None or panel.empty:
            logger.warning("硬过滤输入为空 asof=%s", asof)
            return ScreenResult(asof=asof, passed=[], stats=[])

        snap = self._snapshot(panel, instruments or {}, asof)
        masks: dict[str, pd.Series] = {}
        idx = snap.index

        masks["blacklist"] = ~idx.isin(self.blacklist) if self.blacklist else _true(idx)
        masks["board"] = snap["board"].isin(self.allowed_boards)
        masks["st"] = ~snap["is_st"] if self.exclude_st else _true(idx)
        masks["list_days"] = snap["list_days"] >= self.min_list_days
        masks["suspended"] = ~snap["is_suspended"] if self.exclude_suspended else _true(idx)
        masks["limit_locked"] = (
            ~snap["limit_locked"] if self.exclude_limit_locked else _true(idx)
        )
        masks["amount"] = snap["amount_avg"] >= self.min_amount_20d
        masks["market_cap"] = snap["market_cap"] >= self.min_market_cap
        masks["price"] = (snap["close"] >= self.min_price) & (snap["close"] <= self.max_price)

        # NaN 一律判为"不通过"：数据缺失时宁可漏选也不错选（fail-safe，P4）
        # 顺带统一类型 —— Index.isin() 返回的是裸 ndarray，不是 Series
        for k, m in masks.items():
            s = m if isinstance(m, pd.Series) else pd.Series(m, index=idx)
            masks[k] = s.reindex(idx).fillna(False).astype(bool)

        stats: list[FunnelStage] = []
        rejected: dict[str, str] = {}
        alive = _true(idx)
        for rule in RULE_ORDER:
            m = masks[rule]
            before = int(alive.sum())
            newly_dead = alive & ~m
            for sym in idx[newly_dead]:
                rejected[sym] = _REASON[rule](self, snap.loc[sym])
            alive = alive & m
            stats.append(
                FunnelStage(rule=rule, desc=_DESC[rule](self),
                            before=before, after=int(alive.sum()))
            )

        passed = list(idx[alive])
        detail = pd.DataFrame(masks, index=idx)
        detail["passed"] = alive

        logger.info(
            "L0 硬过滤 asof=%s %d → %d（淘汰 %d）",
            asof, len(idx), len(passed), len(idx) - len(passed),
        )
        return ScreenResult(
            asof=asof, passed=passed, rejected=rejected, stats=stats, detail=detail
        )

    # ------------------------------------------------------------------ 快照
    def _snapshot(self, panel: pd.DataFrame, instruments: dict, asof: date) -> pd.DataFrame:
        """把长表压成"每只票一行"的截面快照。

        所有指标都取 panel 的**最后一根可见 bar**（盘前即 T-1），
        这是整个模块 PIT 正确性的关键：绝不能碰当日数据。
        """
        df = panel.sort_values(["symbol", "date"])
        last = df.groupby("symbol", sort=False).tail(1).set_index("symbol")

        # 20 日均成交额：窗口不足时用现有全部（宁可宽松也不要因新股窗口不足误杀，
        # 上市天数由 list_days 规则单独把关）
        amt = (
            df.groupby("symbol", sort=False)["amount"]
            .apply(lambda s: s.tail(self.amount_window).mean())
            if "amount" in df.columns
            else pd.Series(dtype=float)
        )

        snap = pd.DataFrame(index=last.index)
        snap["close"] = last.get("close", np.nan)
        snap["amount_avg"] = amt.reindex(snap.index) if len(amt) else np.nan
        snap["is_suspended"] = _col_bool(last, "is_suspended")
        snap["limit_locked"] = self._detect_limit_lock(last)
        snap["board"] = [detect_board(s).value for s in snap.index]

        # 标的静态信息
        st_flags, list_days, mcaps, industries = [], [], [], []
        missing = 0
        for sym in snap.index:
            info = instruments.get(sym)
            if info is None:
                missing += 1
                st_flags.append(False)
                list_days.append(10_000)          # 未知上市日 → 不因此淘汰
                mcaps.append(np.nan)
                industries.append("")
                continue
            st_flags.append(bool(getattr(info, "is_st", False)))
            list_days.append(int(info.list_days(asof)) if hasattr(info, "list_days") else 10_000)
            mc = float(getattr(info, "market_cap", 0.0) or 0.0)
            if mc <= 0:  # 券商接口常不给市值，用 收盘价 × 总股本 现算
                mc = float(snap.at[sym, "close"] or 0.0) * float(
                    getattr(info, "total_share", 0.0) or 0.0
                )
            mcaps.append(mc if mc > 0 else np.nan)
            industries.append(str(getattr(info, "industry", "") or ""))

        snap["is_st"] = st_flags
        snap["list_days"] = list_days
        snap["market_cap"] = mcaps
        snap["industry"] = industries

        if missing:
            logger.warning(
                "%d/%d 只标的缺少基础信息，其 ST/上市天数/市值规则按放行处理",
                missing, len(snap),
            )
        # 市值缺失是常态（mock/部分数据源不给），单独降级：全缺则跳过市值闸门
        if snap["market_cap"].isna().all():
            logger.warning("全部标的市值缺失，市值闸门本次跳过")
            snap["market_cap"] = np.inf
        return snap

    def _detect_limit_lock(self, last: pd.DataFrame) -> pd.Series:
        """一字板检测：振幅≈0 且价格贴在涨停或跌停上。

        只用 ``high == low`` 判断会误伤**停牌**票（停牌日 OHLC 常常全等于前收，
        振幅同样是 0，但那不是一字板），所以必须叠加"贴在涨跌停价上"这个条件。
        停牌自有 ``suspended`` 规则处理，两者不能混。

        涨跌停价优先用数据源给的 ``limit_up``/``limit_down`` 列（券商口径最权威，
        已考虑 ST、新股、北交所等特殊规则）；缺列时才退回按板块推算。
        """
        if not {"high", "low"}.issubset(last.columns):
            return pd.Series(False, index=last.index)
        high = pd.to_numeric(last["high"], errors="coerce")
        low = pd.to_numeric(last["low"], errors="coerce")
        close = pd.to_numeric(last["close"], errors="coerce")
        prev = pd.to_numeric(
            last.get("prev_close", pd.Series(np.nan, index=last.index)), errors="coerce"
        ).where(lambda s: s > 0, np.nan)

        flat = ((high - low).abs() / prev) <= self.limit_lock_tol

        if {"limit_up", "limit_down"} <= set(last.columns):
            lu = pd.to_numeric(last["limit_up"], errors="coerce")
            ld = pd.to_numeric(last["limit_down"], errors="coerce")
            # 用相对容差比价，避免不同价位量级下绝对容差失真
            at_limit = (
                ((close - lu).abs() / lu.where(lu > 0, np.nan) <= self.limit_lock_tol)
                | ((close - ld).abs() / ld.where(ld > 0, np.nan) <= self.limit_lock_tol)
            )
        else:
            limits = pd.Series(
                [_limit_pct(s, detect_board(s)) for s in last.index], index=last.index
            )
            at_limit = (close / prev - 1.0).abs() >= (limits - self.limit_lock_tol)

        return (flat & at_limit).fillna(False).astype(bool)


# ---------------------------------------------------------------------- 工具
def _true(idx) -> pd.Series:
    return pd.Series(True, index=idx)


def _col_bool(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(False, index=df.index)
    return df[col].fillna(False).astype(bool)


def _limit_pct(symbol: str, board: Board) -> float:
    if board in (Board.GEM, Board.STAR):
        return 0.20
    if board is Board.BSE:
        return 0.30
    return 0.10


_DESC = {
    "blacklist": lambda s: "黑名单",
    "board": lambda s: f"板块∈{sorted(s.allowed_boards)}",
    "st": lambda s: "排除 ST/*ST",
    "list_days": lambda s: f"上市≥{s.min_list_days}日",
    "suspended": lambda s: "排除停牌",
    "limit_locked": lambda s: "排除一字板",
    "amount": lambda s: f"{s.amount_window}日均额≥{s.min_amount_20d / 1e8:.2f}亿",
    "market_cap": lambda s: f"市值≥{s.min_market_cap / 1e8:.0f}亿",
    "price": lambda s: f"股价∈[{s.min_price:g}, {s.max_price:g}]",
}

_REASON = {
    "blacklist": lambda s, r: "命中黑名单",
    "board": lambda s, r: f"板块 {r['board']} 不在允许范围",
    "st": lambda s, r: "ST/*ST 标的",
    "list_days": lambda s, r: f"上市仅 {int(r['list_days'])} 日 < {s.min_list_days}",
    "suspended": lambda s, r: "停牌",
    "limit_locked": lambda s, r: "一字板（买不进/卖不出）",
    "amount": lambda s, r: (
        f"{s.amount_window}日均额 {_fmt_yi(r['amount_avg'])} < {s.min_amount_20d / 1e8:.2f}亿"
    ),
    "market_cap": lambda s, r: (
        f"市值 {_fmt_yi(r['market_cap'])} < {s.min_market_cap / 1e8:.0f}亿"
    ),
    "price": lambda s, r: f"股价 {r['close']} 超出 [{s.min_price:g}, {s.max_price:g}]",
}


def _fmt_yi(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "NA"
    return "NA" if not np.isfinite(f) else f"{f / 1e8:.2f}亿"
