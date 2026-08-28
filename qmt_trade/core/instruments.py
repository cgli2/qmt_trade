"""标的画像 InstrumentProfile —— 板块差异表。

修正 qmt_etf 的缺陷 #5：它只在选股层（first_board_selector.py）区分了 300/688 的
20% 涨停，**下单层完全没有**，意味着给创业板股票算涨停价时会按 10% 算错。
本模块作为下单层的强制查表，任何价格/数量计算都必须经过它。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

_SYMBOL_RE = re.compile(r"^(?P<code>\d{6})\.(?P<market>SH|SZ|BJ)$", re.IGNORECASE)


class Board(str, Enum):
    MAIN = "MAIN"    # 主板（60/00）
    GEM = "GEM"      # 创业板（300）
    STAR = "STAR"    # 科创板（688）
    BSE = "BSE"      # 北交所（43/83/87/92）
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class InstrumentProfile:
    symbol: str
    board: Board
    name: str = ""
    is_st: bool = False
    is_new_listing: bool = False      # 上市首日/前 5 日，无涨跌幅限制
    limit_pct: float = 0.10           # 涨跌幅限制
    tick_size: float = 0.01           # 最小变动价位
    min_buy_shares: int = 100         # 最小买入数量
    buy_step: int = 100               # 买入数量递增步长
    lot_size: int = 100
    tradable: bool = True             # 本期是否允许交易（北交所/ST 默认排除）
    exclude_reason: str = ""

    # ------------------------------------------------------------ 价格
    def round_price(self, price: float) -> float:
        """按最小变动价位取整。A 股用四舍五入，Python 内置 round 是银行家舍入，必须用 Decimal。"""
        if price is None or not math.isfinite(price):
            raise ValueError(f"非法价格: {price}")
        step = Decimal(str(self.tick_size))
        d = (Decimal(str(price)) / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
        return float(d)

    def limit_up(self, prev_close: float) -> float:
        """涨停价。新股/无限制标的返回一个极大值以示不限制。"""
        if self.is_new_listing:
            return float("inf")
        return self.round_price(prev_close * (1 + self.limit_pct))

    def limit_down(self, prev_close: float) -> float:
        if self.is_new_listing:
            return 0.0
        return self.round_price(prev_close * (1 - self.limit_pct))

    def clip_price(self, price: float, prev_close: float) -> float:
        """把委托价夹逼到涨跌停区间内（Gate-1 的强制要求）。"""
        lo, hi = self.limit_down(prev_close), self.limit_up(prev_close)
        return self.round_price(min(max(price, lo), hi))

    def is_limit_up(self, price: float, prev_close: float, tol: float = 1e-6) -> bool:
        if self.is_new_listing:
            return False
        return price >= self.limit_up(prev_close) - tol

    def is_limit_down(self, price: float, prev_close: float, tol: float = 1e-6) -> bool:
        if self.is_new_listing:
            return False
        return price <= self.limit_down(prev_close) + tol

    # ------------------------------------------------------------ 数量
    def normalize_buy_shares(self, shares: float) -> int:
        """把期望买入股数规整为合法申报量。不足最小申报量返回 0（放弃该笔）。"""
        s = int(shares)
        if s < self.min_buy_shares:
            return 0
        if self.buy_step > 1:
            s = (s // self.buy_step) * self.buy_step
        return s

    def normalize_sell_shares(self, shares: float, available: int) -> int:
        """卖出规整。

        规则：卖出可以不足一手，但若「零股 + 整手」混合，必须整手部分按步长、
        零股部分一次性卖光。简化处理：若要卖的量 >= 可用量，直接全卖；
        否则按 lot_size 向下取整。
        """
        s = int(min(shares, available))
        if s <= 0:
            return 0
        if s >= available:
            return available
        s = (s // self.lot_size) * self.lot_size
        return s


def parse_symbol(symbol: str) -> tuple[str, str]:
    """拆分 ``000001.SZ`` → ``("000001", "SZ")``。支持不带后缀的 6 位代码自动推断。"""
    s = str(symbol).strip().upper()
    m = _SYMBOL_RE.match(s)
    if m:
        return m.group("code"), m.group("market").upper()
    if re.fullmatch(r"\d{6}", s):
        return s, _infer_market(s)
    raise ValueError(f"无法解析的证券代码: {symbol!r}")


def _infer_market(code: str) -> str:
    if code.startswith(("60", "68", "51", "58", "56", "11")):
        return "SH"
    if code.startswith(("43", "83", "87", "92")):
        return "BJ"
    return "SZ"


def normalize_symbol(symbol: str) -> str:
    code, market = parse_symbol(symbol)
    return f"{code}.{market}"


def detect_board(symbol: str) -> Board:
    code, market = parse_symbol(symbol)
    if market == "BJ" or code.startswith(("43", "83", "87", "92")):
        return Board.BSE
    if code.startswith("688"):
        return Board.STAR
    if code.startswith("300") or code.startswith("301"):
        return Board.GEM
    if code.startswith(("60", "00")):
        return Board.MAIN
    return Board.UNKNOWN


#: 板块基础参数表。ST 与新股在 :func:`build_profile` 中二次覆盖。
_BOARD_SPEC: dict[Board, dict] = {
    Board.MAIN: dict(limit_pct=0.10, min_buy_shares=100, buy_step=100, tradable=True),
    Board.GEM: dict(limit_pct=0.20, min_buy_shares=100, buy_step=100, tradable=True),
    # 科创板：单笔申报不小于 200 股，之后可按 1 股递增
    Board.STAR: dict(limit_pct=0.20, min_buy_shares=200, buy_step=1, tradable=True),
    Board.BSE: dict(limit_pct=0.30, min_buy_shares=100, buy_step=1, tradable=False),
    Board.UNKNOWN: dict(limit_pct=0.10, min_buy_shares=100, buy_step=100, tradable=False),
}


def build_profile(
    symbol: str,
    *,
    name: str = "",
    is_st: bool = False,
    is_new_listing: bool = False,
    allowed_boards: set[str] | list[str] | None = None,
) -> InstrumentProfile:
    """构造标的画像。这是全系统获取涨跌停/最小申报量的**唯一入口**。"""
    sym = normalize_symbol(symbol)
    board = detect_board(sym)
    spec = dict(_BOARD_SPEC[board])

    tradable = bool(spec.pop("tradable"))
    reason = "" if tradable else f"{board.value} 板块本期不交易"

    if allowed_boards is not None:
        allowed = {str(b).upper() for b in allowed_boards}
        if board.value not in allowed:
            tradable, reason = False, f"{board.value} 不在允许板块 {sorted(allowed)} 内"

    if is_st:
        # ST 股涨跌幅 5%（科创板/创业板 ST 仍为 20%，这里按主板规则取更严的一方）
        spec["limit_pct"] = 0.05 if board in (Board.MAIN, Board.UNKNOWN) else spec["limit_pct"]
        tradable, reason = False, "ST 标的本期不交易"

    return InstrumentProfile(
        symbol=sym,
        board=board,
        name=name,
        is_st=is_st,
        is_new_listing=is_new_listing,
        tradable=tradable,
        exclude_reason=reason,
        **spec,
    )


class InstrumentRegistry:
    """标的画像缓存。避免每次下单都重新解析代码。"""

    def __init__(self, allowed_boards: set[str] | list[str] | None = None):
        self.allowed_boards = allowed_boards
        self._cache: dict[str, InstrumentProfile] = {}
        self._meta: dict[str, dict] = {}

    def update_meta(self, symbol: str, **meta) -> None:
        """写入 ST / 新股 / 名称等元数据，会使该标的的画像缓存失效。"""
        sym = normalize_symbol(symbol)
        self._meta.setdefault(sym, {}).update(meta)
        self._cache.pop(sym, None)

    def get(self, symbol: str) -> InstrumentProfile:
        sym = normalize_symbol(symbol)
        profile = self._cache.get(sym)
        if profile is None:
            meta = self._meta.get(sym, {})
            profile = build_profile(sym, allowed_boards=self.allowed_boards, **meta)
            self._cache[sym] = profile
        return profile
